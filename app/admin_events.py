from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import queue
import threading
from typing import Iterator


@dataclass(frozen=True)
class AdminEvent:
    event_id: int
    event_type: str
    payload: dict[str, object]


def format_sse_event(event: AdminEvent) -> str:
    payload = json.dumps(event.payload, ensure_ascii=False)
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"


class AdminEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 0
        self._subscribers: set[queue.Queue[AdminEvent]] = set()

    def publish(self, event_type: str, payload: dict[str, object] | None = None) -> AdminEvent:
        with self._lock:
            self._next_id += 1
            event = AdminEvent(self._next_id, event_type, payload or {})
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass
        return event

    @contextmanager
    def subscribe(self) -> Iterator[queue.Queue[AdminEvent]]:
        subscriber: queue.Queue[AdminEvent] = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(subscriber)
        try:
            yield subscriber
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)
