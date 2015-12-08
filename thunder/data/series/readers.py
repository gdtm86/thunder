from numpy import array, arange, frombuffer, load, ndarray, asarray, random

from ...utils.common import check_spark
from .series import Series
spark = check_spark()


def fromrdd(rdd, nrecords=None, index=None, dtype=None):
    """
    Load Series object from a Spark RDD
    """
    from bolt.spark.array import BoltArraySpark

    if index is None or dtype is None:
        item = rdd.values().first()

    if index is None:
        index = range(len(item))

    if dtype is None:
        dtype = item.dtype

    if nrecords is None:
        nrecords = rdd.count()

    values = BoltArraySpark(rdd, shape=(nrecords, len(index)), dtype=dtype, split=1)
    return Series(values, index=index, mode='spark')


def fromlocal(values, index=None):
    """
    Load Series object from a local numpy array.
    """
    values = asarray(values)
    if index is None:
        index = range(len(values[0]))

    return Series(asarray(values), index=index)

def fromlist(items, accessor=None, keys=None, npartitions=None,
             index=None, dtype=None, engine=None):
    """
    Create a Series object from a list.
    """
    if spark and isinstance(engine, spark):
        if dtype is None:
            dtype = accessor(items[0]).dtype if accessor else items[0].dtype
        nrecords = len(items)
        if not keys:
            keys = map(lambda k: (k, ), range(len(items)))
        if not npartitions:
            npartitions = engine.defaultParallelism
        items = zip(keys, items)
        rdd = engine.parallelize(items, npartitions)
        if accessor:
            rdd = rdd.mapValues(accessor)
        return fromrdd(rdd, nrecords=nrecords, index=index, dtype=dtype)

    else:
        if accessor:
            items = [accessor(i) for i in items]
        return fromlocal(items, index=index)

def fromarray(arrays, npartitions=None, index=None, keys=None, engine=None):
    """
    Create a Series object from a sequence of 1d numpy arrays.
    """
    if isinstance(arrays, list):
        arrays = asarray(arrays)

    if isinstance(arrays, ndarray) and arrays.ndim > 1:
        arrays = list(arrays)

    shape = arrays[0].shape
    dtype = arrays[0].dtype
    for ary in arrays:
        if not ary.shape == shape:
            raise ValueError("Inconsistent array shapes: first array had shape %s, "
                             "but other array has shape %s" % (str(shape), str(ary.shape)))
        if not ary.dtype == dtype:
            raise ValueError("Inconsistent array dtypes: first array had dtype %s, "
                             "but other array has dtype %s" % (str(dtype), str(ary.dtype)))

    return fromlist(arrays, keys=keys, npartitions=npartitions, dtype=str(dtype),
                    index=index, engine=engine)

def frommat(path, var, npartitions=None, keyFile=None, index=None, engine=None):
    """
    Loads Series data stored in a Matlab .mat file.
    """
    from scipy.io import loadmat
    data = loadmat(path)[var]
    if data.ndim > 2:
        raise IOError('Input data must be one or two dimensional')
    if keyFile:
        keys = map(lambda x: tuple(x), loadmat(keyFile)['keys'])
    else:
        keys = None

    return fromlist(data, keys=keys, npartitions=npartitions, dtype=str(data.dtype),
                    index=index, engine=engine)

def fromnpy(path, npartitions=None, keyfile=None, index=None, engine=None):
    """
    Loads Series data stored in the numpy save() .npy format.
    """
    data = load(path)
    if data.ndim > 2:
        raise IOError('Input data must be one or two dimensional')
    if keyfile:
        keys = map(lambda x: tuple(x), load(keyfile))
    else:
        keys = None

    return fromlist(data, keys=keys, npartitions=npartitions, dtype=str(data.dtype),
                    index=index, engine=engine)

def fromtext(path, npartitions=None, nkeys=None, ext="txt", dtype='float64', engine=None):
    """
    Loads Series data from text files.

    Parameters
    ----------
    path : string
        Directory to load from, can be a URI string with scheme
        (e.g. "file://", "s3n://", or "gs://"), or a single file,
        or a directory, or a directory with a single wildcard character.

    dtype: dtype or dtype specifier, default 'float64'
        Numerical type to use for data after converting from text.
    """
    if spark and isinstance(engine, spark):
        from thunder.data.readers import normalize_scheme
        path = normalize_scheme(path, ext)

        def parse(line, nkeys_):
            vec = [float(x) for x in line.split(' ')]
            ts = array(vec[nkeys_:], dtype=dtype)
            keys = tuple(int(x) for x in vec[:nkeys_])
            return keys, ts

        lines = engine.textFile(path, npartitions)
        data = lines.map(lambda x: parse(x, nkeys))
        return fromrdd(data, dtype=str(dtype))

    else:
        raise NotImplementedError("Loading not implemented for local mode")

def frombinary(path, ext='bin', conf='conf.json', nkeys=None, nvalues=None,
               keytype=None, valuetype=None, engine=None, credentials=None):
    """
    Load a Series object from a binary files.

    Parameters
    ----------
    path : string URI or local filesystem path
        Directory to load from, can be a URI string with scheme
        (e.g. "file://", "s3n://", or "gs://"), or a single file,
        or a directory, or a directory with a single wildcard character.

    ext : str, optional, default='bin'
        Optional file extension specifier.

    conf : str
        Name of conf file with type and size information.

    nkeys, nvalues : int
        Parameters of binary data, can be specified here or in a configuration file.

    keytype, valuetype : str
        Parameters of binary data, can be specified here or in a configuration file.

    newdtype : dtype, optional, default='float32'
        Numpy dtype to recast output series data to.

    casting : 'no' | 'equiv' | 'safe' | 'same_kind' | 'unsafe', optional, default='safe'
        Casting method to pass on to numpy's `astype()` method.

    """
    params = binaryconfig(path, conf, nkeys, nvalues, keytype, valuetype, credentials)

    from thunder.data.readers import normalize_scheme
    path = normalize_scheme(path, ext)

    from numpy import dtype as dtypeFunc
    keytype = dtypeFunc(params.keytype)
    valuetype = dtypeFunc(params.valuetype)

    keysize = params.nkeys * keytype.itemsize
    recordsize = keysize + params.nvalues * valuetype.itemsize

    if spark and isinstance(engine, spark):
        lines = engine.binaryRecords(path, recordsize)

        def get(kv):
            k = tuple(int(x) for x in frombuffer(buffer(kv, 0, keysize), dtype=keytype))
            v = frombuffer(buffer(kv, keysize), dtype=valuetype)
            return (k, v) if keysize > 0 else v

        raw = lines.map(get)
        if keysize == 0:
            raw = raw.zipWithIndex().map(lambda (v, k): ((k,), v))
        return fromrdd(raw, dtype=str(valuetype), index=arange(params.nvalues))

    else:
        raise NotImplementedError("Loading not implemented for local mode")

def binaryconfig(path, conf, nkeys, nvalues, keytype, valuetype, credentials):
    """
    Collects parameters to use for binary series loading.
    """
    import json
    from collections import namedtuple
    from thunder.data.readers import get_file_reader, FileNotFoundError

    Parameters = namedtuple('BinaryLoadParameters', 'nkeys nvalues keytype valuetype')
    Parameters.__new__.__defaults__ = (None, None, 'int16', 'int16')

    reader = get_file_reader(path)(credentials=credentials)
    try:
        buf = reader.read(path, filename=conf)
        params = json.loads(buf)
    except FileNotFoundError:
        params = {}

    for k in params.keys():
        if k not in Parameters._fields:
            del params[k]
    keywords = {'nkeys': nkeys, 'nvalues': nvalues, 'keytype': keytype, 'valuetype': valuetype}
    for k, v in keywords.items():
        if not v and not v == 0:
            del keywords[k]
    params.update(keywords)
    params = Parameters(**params)

    missing = []
    for name, val in params._asdict().iteritems():
        if not val and not val == 0:
            missing.append(name)
    if missing:
        raise ValueError("Missing parameters to load binary series files - " +
                         "these must be given either as arguments or in a configuration file: " +
                         str(tuple(missing)))
    return params

def fromrandom(shape=(100, 10), npartitions=1, seed=42, engine=None):
    """
    Generate gaussian random series data.

    Parameters
    ----------
    shape : tuple
        Dimensions of data.

    npartitions : int
        Number of partitions with which to distribute data.

    seed : int
        Randomization seed.
    """
    seed = hash(seed)

    def generate(v):
        random.seed(seed + v)
        return random.randn(shape[1])

    return fromlist(range(shape[0]), accessor=generate, npartitions=npartitions, engine=engine)

def fromexample(name=None, engine=None):
    """
    Load example series data.

    Data must be downloaded from S3, so this method requires
    an internet connection.

    Parameters
    ----------
    name : str
        Name of dataset, options include 'iris' | 'mouse' | 'fish'.
        If not specified will print options.
    """
    import os
    import tempfile
    import shutil
    import checkist
    from boto.s3.connection import S3Connection

    datasets = ['iris', 'mouse', 'fish']

    if name is None:
        print 'Availiable example datasets'
        for d in datasets:
            print '- ' + d
        return

    checkist.opts(name, datasets)

    d = tempfile.mkdtemp()
    try:
        os.mkdir(os.path.join(d, 'series'))
        os.mkdir(os.path.join(d, 'series', name))
        conn = S3Connection(anon=True)
        bucket = conn.get_bucket('thunder-sample-data')
        for key in bucket.list(os.path.join('series', name)):
            if not key.name.endswith('/'):
                key.get_contents_to_filename(os.path.join(d, key.name))
        data = frombinary(os.path.join(d, 'series', name), engine=engine)
        data.cache()
        data.compute()
    finally:
        shutil.rmtree(d)

    return data