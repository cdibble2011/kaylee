import random
import marshal
import logging
import gevent
from StringIO import StringIO

import zmq.green as zmq
from collections import defaultdict
from utils import zmq_addr
from backends import RedisShuffler

# States
# ------
START     = 0
MAP       = 1
SHUFFLE   = 2
PARTITION = 3
REDUCE    = 4
COLLECT   = 5

# Shufle Backends
# ---------------
MEMORY   = 0
REDIS    = 1
KAYLEEFS = 2

# Server instructions
# -------------------
#MAP      = 'map'
#REDUCE   = 'reduce'
#DONE     = 'done'
#BYTECODE = 'bytecode'

# Client instructions
# -------------------
CONNECT     = 'connect'
MAPATOM     = 'mapdone'
MAPCHUNK    = 'mapkeydone'
REDUCEATOM  = 'reducedone'

try:
    import msgpack as srl
except ImportError:
    import cPickle as srl

class Server(object):

    def __init__(self, backend=MEMORY):
        self.workers = set()
        self.state = START

        self.backend = backend

        self.mapfn = None
        self.reducefn = None
        self.datafn = None

        self.bytecode = None

        self.started = False
        self.completed = False

        self.working_maps = {}

        logging.basicConfig(logging=logging.DEBUG)
        logging.getLogger("").setLevel(logging.INFO)
        self.logging = logging

    def heartbeat_loop(self):
        for worker in self.workers:
            self.ctrl_socket.send_multipart([worker, 'heartbeat'])
        print 'ping'
        gevent.sleep(1)

    def main_loop(self):
        self.started = True

        poller = zmq.Poller()

        poller.register(self.pull_socket, zmq.POLLIN  | zmq.POLLERR)
        poller.register(self.push_socket, zmq.POLLOUT | zmq.POLLERR)
        poller.register(self.ctrl_socket, zmq.POLLOUT | zmq.POLLERR)

        while self.started and not self.completed:
            try:
                events = dict(poller.poll())
            except zmq.ZMQError:
                self._kill()
                break

            if any(ev & zmq.POLLERR for ev in events.itervalues()):
                self.logging.error('Socket error')
                self._kill()
                break

            # TODO: Specify number of nodes
            if len(self.workers) > 0:
                if events.get(self.push_socket) == zmq.POLLOUT:
                    self.start_new_task()
                if events.get(self.ctrl_socket) == zmq.POLLIN:
                    self.manage()
                if events.get(self.pull_socket) == zmq.POLLIN:
                    self.collect_task()
            else:
                if events.get(self.pull_socket) == zmq.POLLIN:
                    self.collect_task()
                if events.get(self.ctrl_socket) == zmq.POLLIN:
                    self.manage()


    def connect(self, push_addr=None, pull_addr=None, control_addr=None):
        c = zmq.Context()

        # Pull tasks across manager
        pull_addr = zmq_addr(6666, transport='tcp')

        self.pull_socket = c.socket(zmq.PULL)
        self.pull_socket.bind(pull_addr)

        push_addr = zmq_addr(5555, transport='tcp')

        self.push_socket = c.socket(zmq.PUSH)
        self.push_socket.bind(push_addr)

        ctrl_addr = zmq_addr(7777, transport='tcp')

        self.ctrl_socket = c.socket(zmq.ROUTER)
        self.ctrl_socket.bind(ctrl_addr)

    def start(self, timeout=None):
        self.gen_bytecode()
        self.logging.info('Started Server')

        main      = gevent.spawn(self.main_loop)
        heartbeat = gevent.spawn(self.heartbeat_loop)
        gevent.joinall([
            main,
            heartbeat,
        ])

        # Clean exit
        self.done()

    def done(self):
        for worker in self.workers:
            self.ctrl_socket.send_multipart([worker, 'done'])

    def manage(self):
        """
        Manage hearbeats on workers. Keep track of clients that
        are alive.
        """
        msg = self.ctrl_socket.recv_multipart()
        import pdb; pdb.set_trace()

    def _kill(self):
        gr = gevent.getcurrent()
        gr.kill()

    def results(self):
        if self.completed:
            return self.reduce_results
        else:
            return None

    def send_datum(self, command, key, data):
        self.push_socket.send(command, flags=zmq.SNDMORE)
        self.push_socket.send(str(key), flags=zmq.SNDMORE)

        if self.state == MAP:
            self.push_socket.send(data, copy=False)
        else:
            self.push_socket.send(srl.dumps(data))

    def send_command(self, command, payload=None):

        if payload:
            self.send_datum(command, *payload)
        else:
            self.push_socket.send(command)

    def start_new_task(self):
        action = self.next_task()
        if action:
            command, payload = action
            self.send_command(command, payload)

    def next_task(self):
        """
        The main work cycle, does all the distribution.
        """

        # -------------------------------------------
        if self.state == START:
            self.map_iter = self.datafn()

            if self.backend is MEMORY:
                self.map_results = defaultdict(list)
            elif self.backend is REDIS:
                self.map_results = RedisShuffler()
            elif self.backend is KAYLEEFS:
                raise NotImplementedError()

            self.state = MAP
            self.logging.info('Mapping')

        # -------------------------------------------
        if self.state == MAP:
            try:
                map_key, map_item = next(self.map_iter)
                self.working_maps[str(map_key)] = map_item
                return 'map', (map_key, map_item)
            except StopIteration:
                self.logging.info('Shuffling')
                self.state = SHUFFLE

        # -------------------------------------------
        if self.state == SHUFFLE:
            self.reduce_iter = self.map_results.iteritems()
            self.working_reduces = set()
            self.reduce_results = {}

            if len(self.working_maps) == 0:
                self.logging.info('Reducing')
                self.state = PARTITION
            else:
                self.logging.debug('Remaining %s ' % len(self.working_maps))
                #self.logging.debug('Pending chunks %r' % self.working_maps.keys())

        # -------------------------------------------
        if self.state == PARTITION:
            # Normally we would define some sort way to balance the work
            # across workers ( key modulo n ) but ZMQ PUSH/PULL load
            # balances for us.
            self.state = REDUCE

        # -------------------------------------------
        if self.state == REDUCE:
            try:
                reduce_key, reduce_value = next(self.reduce_iter)
                self.working_reduces.add(reduce_key)
                return 'reduce', (reduce_key, reduce_value)
            except StopIteration:
                self.logging.info('Collecting')
                self.state = COLLECT
        # -------------------------------------------

        if self.state == COLLECT:
            if len(self.working_reduces) == 0:
                self.completed = True
                self.logging.info('Finished')
            else:
                self.logging.debug('Still collecting %s' % len(self.working_reduces))

    def collect_task(self):
        # Don't use the results if they've already been counted
        command = self.pull_socket.recv(flags=zmq.SNDMORE)

        if command == 'connect':
            worker_id = self.pull_socket.recv()
            self.send_code(worker_id)

        # Maps Units
        # ==========

        elif command == 'mapkeydone':
            key = self.pull_socket.recv()
            del self.working_maps[key]

        elif command == 'mapdone':
            key = self.pull_socket.recv(flags=zmq.SNDMORE)
            tkey = self.pull_socket.recv(flags=zmq.SNDMORE)
            value = self.pull_socket.recv()

            self.map_results[tkey].extend(value)

        # Reduce Units
        # ============

        elif command == 'reducedone':
            key = self.pull_socket.recv(flags=zmq.SNDMORE)
            value = srl.loads(self.pull_socket.recv())

            # Don't use the results if they've already been counted
            if key not in self.working_reduces:
                return

            self.reduce_results[key] = value
            self.working_reduces.remove(key)

        else:
            raise RuntimeError("Unknown wire chatter")

    def gen_bytecode(self):
        self.bytecode = (
            marshal.dumps(self.mapfn.func_code),
            marshal.dumps(self.reducefn.func_code),
        )

    def gen_llvm(self, mapfn, reducefn):
        mapbc = StringIO()
        reducebc = StringIO()

        mapfn.mod.to_bitcode(mapbc)
        mapfn.mod.to_bitcode(reducebc)

        return (mapbc, reducebc)

    def send_code(self, worker_id):
        # A new worker
        if worker_id not in self.workers:
            self.logging.info('Worker Registered: %s' % worker_id)
            self.workers.add(worker_id)

            payload = ('bytecode', self.bytecode)
            # The worker_id uniquely identifies the worker in
            # the ZMQ topology.
            self.ctrl_socket.send_multipart([worker_id, srl.dumps(payload)])
            self.logging.info('Sending Bytecode to %s' % worker_id)
        else:
            self.logging.debug('Worker asking for code again?')

if __name__ == '__main__':
    # TODO: Support Cython modules
    import sys
    import imp
    import argparse

    path = sys.argv[1]
    imp.load_module(path)

    parser = argparse.ArgumentParser()

    parser.add_argument('--verbose', help='Verbose logging')
    parser.add_argument('--config',  help='Configuration')
    parser.add_argument('--backend', help='Storage backend')

    args = parser.parse_args()

    srv = Server(backend=args['baackend'])
    srv.connect()
