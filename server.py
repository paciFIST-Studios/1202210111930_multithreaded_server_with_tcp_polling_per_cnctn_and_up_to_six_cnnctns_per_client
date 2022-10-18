import queue
from queue import Queue

import select
import socket
import sys
from _thread import *

from network import recv_no_throw


class Server:
    def __init__(self, host=None, port=None):
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

    # util ---------------------------------------------------------------
    def get_addr(self):
        # (ip:port)
        return self.host, self.port

    def prepare(self):
        """Sets up the server for use"""
        self.connections = []

        # gets os to say if platform supports dual-stack
        use_dual_stack = socket.has_dualstack_ipv6()
        print(f'dual_stack={use_dual_stack}')

        self.server_socket = socket.create_server(
            self.get_addr(),
            family=socket.AF_INET6,
            dualstack_ipv6=use_dual_stack,
            reuse_port=True)
        self.server_socket.setblocking(False)
        self.server_socket.listen()

        # this queue is for the server's listen socket
        self.message_queues = {self.server_socket.getsockname(): Queue()}
        print(f'Server({self.host}:{self.port})')

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

    # handlers -----------------------------------------------------------
    def handle_add_new_connection(self, listen_socket):
        new_connection, client_address = listen_socket.accept()
        new_connection.setblocking(False)
        self.connections.append(new_connection)

        print(f'incoming connection {client_address}, conns={len(self.connections)}')
        new_connection.sendall('ACK: connection accepted by server\n'.encode())

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
        print(f'read: {peer}')
        data = recv_no_throw(data_socket)
        print(f'server received: "{data}"')
        if not data:
            return

        line = data.decode('utf-8').strip()
        if line.endswith('close'):
            if data_socket in self.connections[:]:
                data_socket.sendall('closing connection\n'.encode())
                self._close_and_remove_socket(data_socket)
        if peer in self.message_queues:
            self.message_queues[peer].put(f'performed command: {line}\n')

    def handle_socket_write(self, write_socket):
        # socket is None
        if not write_socket:
            return
        # socket is closed, but not destroyed
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
            write_socket.sendall(next_message.encode())
            self.message_queues[peer].task_done()

    def handle_socket_exception(self, exception_socket):
        print(f'except: ({exception_socket.getsockname()})')
        self._close_and_remove_socket(exception_socket)

    # --------------------------------------------------------------------
    def run(self):
        print(f'beginning server loop')
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
                c.sendall('server close\n'.encode())
                self._close_and_remove_socket(c)
        if self.server_socket:
            self.server_socket.close()


def run():
    print('starting server')
    s = Server('localhost', 9001)
    s.prepare()
    s.run()


if __name__ == '__main__':
    run()

