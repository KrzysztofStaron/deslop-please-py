from typing import Union

from .ring import Ring


def by_loop(rings: list[Ring]) -> None:
    for ring in rings:
        ring.items.clear()


def by_mapping(rings: dict[str, Ring]) -> int:
    total = 0
    for name, ring in rings.items():
        total += ring.head
    for ring in rings.values():
        total += len(ring.items)
    return total


def by_narrowing(thing: Union[Ring, int]) -> None:
    if isinstance(thing, Ring):
        thing.head = 0


def by_comprehension(rings: list[Ring]) -> list[int]:
    return [r.head for r in rings]
