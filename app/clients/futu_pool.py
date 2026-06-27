"""Thread-safe connection pool for Futu OpenD OpenQuoteContext."""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

RET_OK = 0
_IDLE_SWEEP_INTERVAL_SEC = 60.0


@dataclass
class _PooledConnection:
    ctx: Any
    last_used: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.monotonic)


class FutuConnectionPool:
    """Fixed-size reusable pool of Futu OpenQuoteContext connections."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        min_size: int = 2,
        max_size: int = 8,
        idle_timeout_sec: int = 300,
        acquire_timeout_sec: float = 30.0,
        connect_timeout_seconds: float = 0.3,
    ) -> None:
        if min_size < 0:
            raise ValueError("min_size must be >= 0")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if min_size > max_size:
            raise ValueError("min_size cannot exceed max_size")

        self._host = host
        self._port = port
        self._min_size = min_size
        self._max_size = max_size
        self._idle_timeout_sec = idle_timeout_sec
        self._acquire_timeout_sec = acquire_timeout_sec
        self._connect_timeout_seconds = connect_timeout_seconds

        self._idle: queue.Queue[_PooledConnection] = queue.Queue()
        self._lock = threading.Lock()
        self._total = 0
        self._closed = False
        self._sweeper_stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        host: str | None = None,
        port: int | None = None,
    ) -> "FutuConnectionPool":
        return cls(
            host=host or settings.futu_host,
            port=port or settings.futu_port,
            min_size=settings.futu_pool_min,
            max_size=settings.futu_pool_max,
            idle_timeout_sec=settings.futu_pool_idle_sec,
            acquire_timeout_sec=settings.futu_pool_acquire_timeout_sec,
            connect_timeout_seconds=settings.futu_connect_timeout_seconds,
        )

    def start(self) -> None:
        """Warm min connections and start idle sweeper."""
        if self._closed:
            return
        self._warmup()
        if self._sweeper is None or not self._sweeper.is_alive():
            self._sweeper_stop.clear()
            self._sweeper = threading.Thread(
                target=self._idle_sweep_loop,
                name="futu-pool-sweeper",
                daemon=True,
            )
            self._sweeper.start()

    def _warmup(self) -> None:
        for _ in range(self._min_size):
            try:
                with self._lock:
                    if self._closed or self._total >= self._max_size:
                        break
                    pooled = self._create_locked()
                self._idle.put(pooled)
            except Exception as exc:
                logger.warning("Futu pool warmup failed: %s", exc)
                break
        logger.info(
            "Futu pool warmed host=%s:%s idle=%s total=%s min=%s max=%s",
            self._host,
            self._port,
            self._idle.qsize(),
            self._total,
            self._min_size,
            self._max_size,
        )

    def _ensure_reachable(self) -> None:
        try:
            with socket.create_connection(
                (self._host, self._port),
                timeout=self._connect_timeout_seconds,
            ):
                return
        except OSError as exc:
            raise RuntimeError(f"futu_opend_unreachable: {self._host}:{self._port}") from exc

    def _create_locked(self) -> _PooledConnection:
        self._ensure_reachable()
        try:
            from futu import OpenQuoteContext
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"futu_sdk_unavailable: {exc}") from exc
        ctx = OpenQuoteContext(host=self._host, port=self._port)
        self._total += 1
        now = time.monotonic()
        return _PooledConnection(ctx=ctx, last_used=now, created_at=now)

    def _is_healthy(self, pooled: _PooledConnection) -> bool:
        try:
            ret, _payload = pooled.ctx.get_global_state()
            return ret == RET_OK
        except Exception as exc:
            logger.debug("Futu pool health check failed: %s", exc)
            return False

    def _discard(self, pooled: _PooledConnection) -> None:
        close = getattr(pooled.ctx, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logger.debug("Futu pool close failed: %s", exc)
        with self._lock:
            self._total = max(0, self._total - 1)

    def _replenish_min_locked(self) -> None:
        while self._total < self._min_size and not self._closed:
            try:
                pooled = self._create_locked()
            except Exception as exc:
                logger.warning("Futu pool replenish failed: %s", exc)
                break
            self._idle.put(pooled)

    def acquire(self) -> _PooledConnection:
        if self._closed:
            raise RuntimeError("futu_pool_closed")

        deadline = time.monotonic() + self._acquire_timeout_sec
        while True:
            pooled = self._try_take_idle()
            if pooled is not None:
                return pooled

            with self._lock:
                if not self._closed and self._total < self._max_size:
                    return self._create_locked()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("futu_pool_exhausted")

            try:
                pooled = self._idle.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue

            if self._is_healthy(pooled):
                pooled.last_used = time.monotonic()
                return pooled
            self._discard(pooled)

    def release(self, pooled: _PooledConnection) -> None:
        if self._closed:
            self._discard(pooled)
            return

        if not self._is_healthy(pooled):
            self._discard(pooled)
            with self._lock:
                self._replenish_min_locked()
            return

        pooled.last_used = time.monotonic()
        self._idle.put(pooled)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        pooled = self.acquire()
        try:
            yield pooled.ctx
        finally:
            self.release(pooled)

    def _try_take_idle(self) -> _PooledConnection | None:
        while True:
            try:
                pooled = self._idle.get_nowait()
            except queue.Empty:
                return None
            if self._is_healthy(pooled):
                pooled.last_used = time.monotonic()
                return pooled
            self._discard(pooled)

    def _idle_sweep_loop(self) -> None:
        while not self._sweeper_stop.wait(_IDLE_SWEEP_INTERVAL_SEC):
            if self._closed:
                break
            self._sweep_idle()

    def _sweep_idle(self) -> None:
        now = time.monotonic()
        kept: list[_PooledConnection] = []
        while True:
            try:
                pooled = self._idle.get_nowait()
            except queue.Empty:
                break
            idle_for = now - pooled.last_used
            with self._lock:
                over_min = self._total > self._min_size
            if idle_for >= self._idle_timeout_sec and over_min:
                self._discard(pooled)
            else:
                kept.append(pooled)
        for pooled in kept:
            self._idle.put(pooled)
        with self._lock:
            self._replenish_min_locked()

    def close_all(self) -> None:
        self._closed = True
        self._sweeper_stop.set()
        sweeper = self._sweeper
        if sweeper is not None and sweeper.is_alive() and sweeper is not threading.current_thread():
            sweeper.join(timeout=2.0)

        while True:
            try:
                pooled = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(pooled)

        logger.info("Futu pool closed host=%s:%s", self._host, self._port)

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            total = self._total
        return {
            "total": total,
            "idle": self._idle.qsize(),
            "in_use": max(0, total - self._idle.qsize()),
            "min_size": self._min_size,
            "max_size": self._max_size,
        }


_pools: dict[tuple[str, int], FutuConnectionPool] = {}
_pool_lock = threading.Lock()


def get_futu_pool(
    *,
    host: str | None = None,
    port: int | None = None,
    start: bool = True,
) -> FutuConnectionPool:
    settings = get_settings()
    resolved_host = host or settings.futu_host
    resolved_port = port or settings.futu_port
    key = (resolved_host, resolved_port)
    with _pool_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = FutuConnectionPool.from_settings(settings, host=resolved_host, port=resolved_port)
            _pools[key] = pool
            if start and settings.futu_enabled and key == (settings.futu_host, settings.futu_port):
                pool.start()
        return pool


def close_futu_pool() -> None:
    with _pool_lock:
        for pool in _pools.values():
            pool.close_all()
        _pools.clear()


def reset_futu_pool_for_tests() -> None:
    """Tear down singleton pool (tests only)."""
    close_futu_pool()
