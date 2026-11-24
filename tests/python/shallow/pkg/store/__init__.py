from .table import width


class Store:
    def __init__(self) -> None:
        self._rows: dict[str, int] = {}
        self._width = width()

    def a(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def b(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def c(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def d(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def e(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def f(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def g(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def h(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]

    def i(self, key: str) -> int:
        self._rows[key] = self._rows.get(key, 0) + self._width
        return self._rows[key]
