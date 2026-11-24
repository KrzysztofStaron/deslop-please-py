from .kinds import BACKLOG_MOST


class Stream:
    def __init__(self, port: int) -> None:
        self.port = port
        self.backlog = BACKLOG_MOST
