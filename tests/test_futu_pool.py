from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest


class FakeCtx:
    instances: list["FakeCtx"] = []
    live = 0
    lock = threading.Lock()

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        with FakeCtx.lock:
            FakeCtx.live += 1
            FakeCtx.instances.append(self)
        self.host = host
        self.port = port
        self.closed = False
        self.calls = 0

    def get_global_state(self):
        if self.closed:
            return -1, "closed"
        return 0, {"qot_logined": "1"}

    def get_option_screen(self, _request):
        self.calls += 1
        time.sleep(0.05)
        return 0, (True, 1, [])

    def close(self) -> None:
        self.closed = True
        with FakeCtx.lock:
            FakeCtx.live -= 1


@pytest.fixture(autouse=True)
def _reset_fake_ctx() -> None:
    FakeCtx.instances.clear()
    FakeCtx.live = 0
    from app.clients.futu_pool import reset_futu_pool_for_tests

    reset_futu_pool_for_tests()
    yield
    reset_futu_pool_for_tests()


def _make_pool(**kwargs):
    from app.clients.futu_pool import FutuConnectionPool

    defaults = dict(
        host="127.0.0.1",
        port=59999,
        min_size=2,
        max_size=4,
        idle_timeout_sec=300,
        acquire_timeout_sec=2.0,
        connect_timeout_seconds=0.05,
    )
    defaults.update(kwargs)
    pool = FutuConnectionPool(**defaults)
    pool._ensure_reachable = lambda: None  # type: ignore[method-assign]
    pool._create_locked = lambda: pool._create_fake()  # type: ignore[method-assign]

    def _create_fake():
        from app.clients.futu_pool import _PooledConnection

        pool._total += 1
        now = time.monotonic()
        return _PooledConnection(ctx=FakeCtx(pool._host, pool._port), last_used=now, created_at=now)

    pool._create_fake = _create_fake  # type: ignore[attr-defined]
    return pool


def test_pool_reuses_connections() -> None:
    pool = _make_pool(min_size=1, max_size=2)
    pool.start()

    with pool.connection() as ctx1:
        id1 = id(ctx1)
    with pool.connection() as ctx2:
        id2 = id(ctx2)

    assert id1 == id2
    assert FakeCtx.live == 1
    assert pool.stats["total"] == 1


def test_pool_caps_max_connections() -> None:
    pool = _make_pool(min_size=0, max_size=2)
    barrier = threading.Barrier(3)
    errors: list[str] = []

    def worker() -> None:
        try:
            pooled = pool.acquire()
            barrier.wait(timeout=1)
            time.sleep(0.1)
            pool.release(pooled)
        except Exception as exc:
            errors.append(str(exc))

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(worker) for _ in range(3)]
        for future in as_completed(futures):
            future.result()

    assert FakeCtx.live <= 2
    assert pool.stats["max_size"] == 2


def test_concurrent_option_screen_board_uses_pool(monkeypatch) -> None:
    from app.clients import futu_pool as pool_mod
    from app.clients.futu_client import FutuQuoteClient

    pool = _make_pool(min_size=2, max_size=4, acquire_timeout_sec=5.0)
    pool.start()
    monkeypatch.setitem(pool_mod._pools, ("127.0.0.1", 11111), pool)

    client = FutuQuoteClient(enabled=True, host="127.0.0.1", port=11111)

    def run_board() -> dict:
        return client.get_option_screen_board(sort_indicator="VOLUME", limit=10)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: run_board(), range(8)))

    assert all(not r.get("error") for r in results)
    assert FakeCtx.live <= 4
    assert pool.stats["total"] <= 4


def test_pool_discards_unhealthy_on_release() -> None:
    pool = _make_pool(min_size=1, max_size=2)
    pool.start()
    pooled = pool.acquire()

    def bad_health(_pooled) -> bool:
        return False

    pool._is_healthy = bad_health  # type: ignore[method-assign]
    pool.release(pooled)
    assert FakeCtx.live == 1
    assert pool.stats["total"] == 1
