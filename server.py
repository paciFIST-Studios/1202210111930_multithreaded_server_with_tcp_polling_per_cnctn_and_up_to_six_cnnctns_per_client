import queue
from queue import Queue

import select
import socket
from _thread import *


class Server:
    def __del__(self):
        if self.server_socket:
            self.server_socket.close()

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

    def get_addr(self):
        # (ip:port)
        return self.host, self.port

    def prepare(self):
        self.connections = []
        use_dual_stack = socket.has_dualstack_ipv6()
        print(f'dual_stack={use_dual_stack}')
        self.server_socket = socket.create_server(
            self.get_addr(),
            family=socket.AF_INET6,
            dualstack_ipv6=use_dual_stack,
            reuse_port=True)
        self.server_socket.listen()
        self.connections.append(self.server_socket)
        self.message_queues = {self.server_socket.getsockname(): Queue()}
        print(f'Server({self.host}:{self.port})')

    def handle_add_new_connection(self, incoming):
        connection, address = incoming.accept()
        connection.setblocking(0)
        print(f'incoming connection {address}')

        self.connections.append(incoming)
        self.message_queues[incoming.getsockname()] = Queue()
        return True

    def handle_socket_read(self, incoming):
        inc = incoming.getsockname()
        srv = self.server_socket.getsockname()
        if inc == srv:
            self.handle_add_new_connection(incoming)
            return

        data = incoming.recv(1024)
        if data:
            self.message_queues[incoming.getsockname()].put(data)
            return incoming

        # # when removing dropped clients
        # client_quit = False
        # if client_quit:
        #     self.connections.remove(incoming)

    def handle_socket_write(self, outgoing):
        try:
            next_message = self.message_queues[outgoing.getsockname()].get_nowait()
        except queue.Empty:
            self.connections.remove(outgoing)
        else:
            outgoing.send(next_message)

    def handle_socket_exception(self, socket):
        if socket in self.connections:
            self.connections.remove(socket)
            socket.close()
            del self.message_queues[socket.getsockname()]

    def run(self):
        print(f'beginning server loop')
        while not self.break_loop:
            # we take a copy of our current state to work from
            # when servicing these socket connections
            read_list = self.connections[:]
            write_list = []
            exceptions = self.connections[:]

            timeout_s = 0.001  # 1 ms
            _read, _write, _except = select.select(
                read_list, write_list, exceptions, timeout_s)

            for readable_socket in _read:
                add_to_write_list = self.handle_socket_read(readable_socket)
                write_list.append(add_to_write_list)
            for writable_socket in _write:
                self.handle_socket_write(writable_socket)
            # for exception in exceptions:
            #     self.handle_socket_exception(exception)


def run():
    print('starting server')
    s = Server('localhost', 9001)
    s.prepare()
    s.run()


if __name__ == '__main__':
    run()

