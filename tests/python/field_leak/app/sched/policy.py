from ..queue.ring import RunQueue


def admit(queue: RunQueue, item: int) -> None:
    queue.items.append(item)


def next_item(queue: RunQueue) -> int:
    item = queue.items[queue.head]
    queue.head += 1
    return item
