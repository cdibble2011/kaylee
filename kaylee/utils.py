import time
import msgpack

transports = frozenset(['udp', 'tcp', 'ipc', 'inproc'])

def cat(*xs):
    return "".join(xs)

def print_timing(func):
  def wrapper(*arg):
    t1 = time.time()
    res = func(*arg)
    t2 = time.time()
    print '%s took %0.3f ms' % (func.func_name, (t2-t1)*1000.0)
    return res
  return wrapper

def sub_subscription_prefix(worker_id, n=3):
    """
    Listen for n-tuples with the worker id prefix without
    deserialization. Very fast for PUB/SUB.
    """
    return msgpack.dumps(tuple([worker_id] + [None]*(n-1)))[0:2]

def zmq_addr(port, transport=None, host=None):
    if host is None:
        host = '127.0.0.1'

    if transport is None:
        transport = 'tcp'

    assert transport in transports
    assert 1000 < port < 10000

    return '{transport}://{host}:{port}'.format(
        transport = transport,
        host      = host,
        port      = port,
    )
