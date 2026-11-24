from .ring import Ring


def run() -> int:
    ring = Ring(4)
    ring.push(1)
    return ring.backlog()
