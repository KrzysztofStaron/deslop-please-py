from .timers import Timers


class Tasks:
    def __init__(self) -> None:
        self.timers = Timers()
        self.ready: list[int] = []

    def spawn(self, task: int) -> None:
        self.ready.append(task)
        self.timers.arm(task)
