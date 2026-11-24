from .store import Store


def run() -> int:
    store = Store()
    total = 0
    for name in "abcdefghi":
        total += getattr(store, name)("k")
    store.a("x")
    store.b("x")
    store.c("x")
    store.d("x")
    store.e("x")
    store.f("x")
    store.g("x")
    store.h("x")
    store.i("x")
    return total
