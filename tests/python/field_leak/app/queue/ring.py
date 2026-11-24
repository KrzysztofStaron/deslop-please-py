class RunQueue:
    def __init__(self, limit: int) -> None:
        self.items: list[int] = []
        self.head = 0
        self.limit = limit

    def push(self, item: int) -> bool:
        if len(self.items) - self.head >= self.limit:
            return False
        self.items.append(item)
        return True
