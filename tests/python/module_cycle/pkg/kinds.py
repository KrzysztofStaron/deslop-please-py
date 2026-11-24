from . import socket

BACKLOG_MOST = 128


def listen(port: int) -> "socket.Stream":
    return socket.Stream(port)
