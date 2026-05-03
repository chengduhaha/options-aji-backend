"""Simple in-process fan-out for SSE market alerts."""

from __future__ import annotations

import asyncio

class EventBroadcaster:
    """Retain weak client queues; callers must guard publish failures gracefully."""

    def __init__(self, *, queue_maxsize: int = 32) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: list[asyncio.Queue[dict[str, object]]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, target: asyncio.Queue[dict[str, object]]) -> None:
        async with self._lock:
            self._subscribers = [q for q in self._subscribers if q is not target]

    async def publish(self, payload: dict[str, object]) -> None:
        async with self._lock:
            copy = list(self._subscribers)
        for subscriber in copy:
            try:
                subscriber.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    _ = subscriber.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                subscriber.put_nowait(payload)


broker = EventBroadcaster()
