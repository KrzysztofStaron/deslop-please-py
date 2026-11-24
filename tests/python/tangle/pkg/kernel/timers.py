from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import Tasks


class Timers:
    def __init__(self) -> None:
        self.armed: list[int] = []

    def arm(self, task: int) -> None:
        self.armed.append(task)

    def fire(self, tasks: "Tasks") -> None:
        for task in self.armed:
            tasks.spawn(task)
