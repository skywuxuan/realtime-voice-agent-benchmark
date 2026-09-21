"""Bounded, all-or-nothing fanout. Use only from one asyncio event loop."""

import asyncio

from events.schema import EventDraft


class BackpressureError(RuntimeError):
    pass


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, asyncio.Queue[EventDraft]] = {}
        self.high_watermarks: dict[str, int] = {}

    def subscribe(self, name: str, *, capacity: int = 256) -> asyncio.Queue[EventDraft]:
        if name in self._subscribers or capacity <= 0:
            raise ValueError("subscriber needs a unique name and positive capacity")
        queue: asyncio.Queue[EventDraft] = asyncio.Queue(maxsize=capacity)
        self._subscribers[name] = queue
        self.high_watermarks[name] = 0
        return queue

    def publish(self, event: EventDraft) -> None:
        full = [name for name, queue in self._subscribers.items() if queue.full()]
        if full:
            raise BackpressureError(f"event not delivered; queues full: {', '.join(full)}")
        # No awaits between the capacity check and delivery: no partial fanout.
        for name, queue in self._subscribers.items():
            queue.put_nowait(event)
            self.high_watermarks[name] = max(self.high_watermarks[name], queue.qsize())
