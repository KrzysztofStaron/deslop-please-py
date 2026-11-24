class Ring:
    def __init__(self, limit: int) -> None:
        self.items: list[int] = []
        self.limit = limit

    def push(self, item: int) -> bool:
        if len(self.items) >= self.limit:
            return False
        self.items.append(item)
        return True

    def backlog(self) -> int:
        return len(self.items)
