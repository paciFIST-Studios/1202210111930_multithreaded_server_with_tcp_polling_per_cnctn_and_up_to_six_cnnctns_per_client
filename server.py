import ctypes
from enum import IntEnum
import queue
from queue import Queue

import select
import socket
import sys
from _thread import *


class MessageType(IntEnum):
    # a new connection occurs, and requests to handshake with server
    HANDSHAKE_REQUEST = 1
    # server response to handshake, if accepted
    HANDSHAKE_OK = 2

    # connection used unsupported protocol version
    PROTOCOL_NOT_SUPPORTED = 3

    # server cannot accommodate any additional connections
    SERVER_FULL = 4
    # a connection is requesting authority
    ASSUME_CONTROL = 5
    # server agrees to give connection authority
    AUTHORITY_GRANTED = 6

    # connection notifies the server that it is going to drop
    NOTIFY_CLOSE = 7

    # other clients are notified by the server, of a new player joining
    NEW_PLAYER_JOIN = 8
    # other clients are notified that a player is dropping their connection
    PLAYER_LEAVE = 9
    # player input is sent to other clients from server
    PLAYER_INPUT = 10


class MessageHeaderBits(ctypes.LittleEndianStructure):
    VERSION_MAJOR = 0
    VERSION_MINOR = 1

    _fields_ = [
        ('type', ctypes.c_uint8, 8),
        ('flags', ctypes.c_uint8, 8),
        ('data', ctypes.c_uint16, 16),
    ]


class MessageHeader(ctypes.Union):
    _fields_ = [
        ('b', MessageHeaderBits),
        ('data', ctypes.c_uint32)
    ]


class ConnectionGroup:
    def __init__(self):
        self.connections = []
        self._last_used_idx = 0

    def add_connection(self, client_socket):
        if client_socket not in self.connections:
            self.connections.append(client_socket)

    def remove_connection(self, client_socket):
        if client_socket in self.connections:
            self.connections.remove(client_socket)

    def send(self, message):
        if not self.connections:
            return
        self.connections[self._last_used_idx].safe_send(message)
        # advance te index to the next message, w/ wrap
        self._last_used_idx += 1
        self._last_used_idx = self._last_used_idx % len(self.connections)


class Server:
    def print_debug(self, message):
        if not self.debug:
            return
        print(message)

    def __init__(self, host=None, port=None, debug=False):
        self.server_socket = None
        self.socket_is_bound = False
        self.socket_is_listening = False
        self.host = host
        self.port = port
        self.thread_count = 0
        self.connections = None
        self.connection_timeout_s = 0.001
        self.message_queues = None
        self.break_loop = False

        self.debug = debug

    # util ---------------------------------------------------------------
    # https://stackoverflow.com/a/265491/20191581
    @staticmethod
    def set_bit(source, idx, set_to_one):
        if set_to_one:
            return source | 2 ** idx
        else:
            return source & ~(2 ** idx)

    @staticmethod
    def bit_is_set(source, idx):
        if idx < 0:
            idx = 0
        # 2^k is the same as setting idx bit to 1
        test_bit = 2 ** idx
        return (source & test_bit) > 0

    @staticmethod
    def get_header(type_uint8, flags_uint8, data_uint16):
        header = MessageHeader()
        header.b.type = type_uint8
        header.b.flags = flags_uint8
        header.b.data = data_uint16
        return header

    def get_addr(self):
        # (ip:port)
        return self.host, self.port

    def prepare(self):
        """Sets up the server for use"""

        self.connections = []

        # gets os to say if platform supports dual-stack
        use_dual_stack = socket.has_dualstack_ipv6()
        self.print_debug(f'dual_stack={use_dual_stack}')

        self.server_socket = socket.create_server(
            self.get_addr(),
            family=socket.AF_INET6,
            dualstack_ipv6=use_dual_stack,
            reuse_port=True)
        self.server_socket.setblocking(False)
        # turn off Nagel's Algorithm (which is slow in the case of using many tiny packets)
        self.server_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
        self.server_socket.listen()

        # this queue is for the server's listen socket
        self.message_queues = {self.server_socket.getsockname(): Queue()}
        self.print_debug(f'Server({self.host}:{self.port})')

    def _close_and_remove_socket(self, socket):
        peer = socket.getpeername()
        if socket in self.connections:
            self.connections.remove(socket)
        socket.close()
        if peer in self.message_queues:
            del self.message_queues[peer]

    # local server commands ----------------------------------------------
    # WINDOWS NOT SUPPORTED
    def _handle_local_server_command(self):
        def _print_line(count, char):
            print(char * count)

        def _print_help():
            _print_line('-', 80)
            print('server commands:')
            print('\thelp\t-\tdisplays this message')
            print('\tclose\t-\tcloses all connections and shuts down server')
            print('\tusers\t-\tprints a list of the connected users')
            print('\tqueues\t-\tprints out existing queue data for all connections')
            _print_line('-', 80)

        def _print_connections(connections):
            _print_line('-', 80)
            if not connections:
                print('\tNo Connections')
            else:
                print('connections:')
                for connection in connections:
                    if connection == self.server_socket:
                        print(f'server socket: {self.server_socket.getsockname()}')
                        continue
                    else:
                        print(f'peer socket: {connection.getpeername()}')
            _print_line('-', 80)

        def _print_server_status():
            _print_line('-', 80)
            print('server status:')
            print(f'\tpython\t-\t{sys.version}')
            print(f'\tthread\t-\t{sys.thread_info}')
            print(f'\tplatform -\t{sys.platform}')
            print(f'\tallocated -\t{sys.getallocatedblocks()} blocks')
            _print_line('-', 80)

        def _print_queues(queues):
            _print_line('-', 80)
            for q in queues:
                print(f'queue:  {q}')
            _print_line('-', 80)

        line = sys.stdin.readline().strip()
        print(f'local command: "{line}"')

        if line.startswith('help'):
            _print_help()
        elif line.startswith('close'):
            print('closing server')
            self.break_loop = True
        elif line.startswith('users'):
            _print_connections(self.connections[:])
        elif line.startswith('status'):
            _print_server_status()
        elif line.startswith('queues'):
            _print_queues(self.message_queues)
        elif line.startswith('clear'):
            _print_line('\n', 50)

    # network fns --------------------------------------------------------
    @staticmethod
    def safe_recv(recv_socket, message_length=1024):
        chunks = []
        try:
            bytes_received = 0
            while bytes_received < message_length:
                read_length = min(message_length - bytes_received, message_length)
                chunk = recv_socket.recv(read_length)
                if chunk == b'':
                    break
                chunks.append(chunk)
                bytes_received += len(chunk)
        except socket.error:
            pass
        return b''.join(chunks)

    @staticmethod
    def safe_send(send_socket, message):
        total_sent = 0
        message_length = len(message)
        while total_sent < message_length:
            sent_length = send_socket.send(message[total_sent:])
            if not sent_length:
                break
            total_sent += sent_length

    # handlers -----------------------------------------------------------
    def handle_add_new_connection(self, listen_socket):
        new_connection, client_address = listen_socket.accept()
        new_connection.setblocking(False)
        self.connections.append(new_connection)

        self.print_debug(f'incoming connection {client_address}, conns={len(self.connections)}')
        header = self.get_header(MessageType.HANDSHAKE_OK, 0, 0)
        self.safe_send(new_connection, f'{header.data}ACK: connection accepted by server\n'.encode())

        peer = new_connection.getpeername()
        self.message_queues[peer] = Queue()
        self.message_queues[peer].put('RESPONSE FROM SERVER\n')

    def handle_socket_read(self, read_socket):
        if read_socket == sys.stdin:
            self._handle_local_server_command()
            return

        # socket reads which occur on the server listen socket, are only
        # used to add new connections, so all reads from that socket
        # can be presumed to contain, only new connection requests
        if read_socket is self.server_socket:
            self.handle_add_new_connection(read_socket)
            return

        # otherwise, this socket isn't the server socket, and there's
        # data incoming.  It should go ot a handle_data fn
        self.handle_receive_data(read_socket)

    def handle_receive_data(self, data_socket):
        peer = data_socket.getpeername()
        data = self.safe_recv(data_socket)
        self.print_debug(f'server received: "{data}" from: {peer}')
        if not data:
            return

        line = data.decode('utf-8').strip()
        if line.endswith('close'):
            if data_socket in self.connections[:]:
                self.safe_send(data_socket, 'closing connection\n'.encode())
                self._close_and_remove_socket(data_socket)
        elif line.endswith('close-server'):
            self.break_loop = True
        elif line.endswith('upgrade'):
            self.print_debug(f'upgrading connection with {peer}')
        if peer in self.message_queues:
            self.message_queues[peer].put(f'performed command: {line}\n')

    def handle_socket_write(self, write_socket):
        # socket is None
        if not write_socket:
            return
        # socket is closed, but not destroyed (yet?)
        if write_socket.fileno() == -1:
            return

        peer = write_socket.getpeername()

        try:
            next_message = self.message_queues[peer].get_nowait()
        except queue.Empty:
            # we don't want to automatically drop a connection if the queue is empty
            # we anticipate all sockets to notify the server when they intend to close
            pass
        else:
            self.safe_send(write_socket, next_message.encode())
            self.message_queues[peer].task_done()

    def handle_socket_exception(self, exception_socket):
        self.print_debug(f'except: ({exception_socket.getsockname()})')
        self._close_and_remove_socket(exception_socket)

    # --------------------------------------------------------------------
    def run(self):
        self.print_debug(f'beginning server loop')
        while not self.break_loop:
            # we'll read from every socket that's connected to us
            possible_reads = self.connections[:]
            # AND, we'll read from the server's listen socket, for new connections
            possible_reads.insert(0, self.server_socket)
            # NOT SUPPORTED ON WINDOWS
            # AND, we'll support some command line interactions
            possible_reads.insert(1, sys.stdin)

            # we'll write to every socket that's connected to us,
            possible_writes = self.connections[:]
            # and handle exceptions for them
            possible_exceptions = self.connections[:]

            timeout_s = 0.004  # 4 ms
            _read, _write, _except = select.select(
                possible_reads,
                possible_writes,
                possible_exceptions,
                timeout_s)

            for readable_socket in _read:
                self.handle_socket_read(readable_socket)
            for writable_socket in _write:
                self.handle_socket_write(writable_socket)
            for exception in _except:
                self.handle_socket_exception(exception)

        # after loop
        if self.connections:
            for c in self.connections[:]:
                # notify user that server is closing connection
                self.safe_send(c, 'server close\n'.encode())
                self._close_and_remove_socket(c)
        if self.server_socket:
            self.server_socket.close()


def run():
    debug = True
    if debug:
        print('starting server')
    s = Server('localhost', 9001, debug)
    s.prepare()
    s.run()


if __name__ == '__main__':
    run()

