from ..queue.ring import RunQueue


def backlog(queue: RunQueue) -> int:
    return len(queue.items) - queue.head
