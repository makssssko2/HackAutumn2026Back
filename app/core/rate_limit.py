from collections import deque
from threading import Lock
from time import monotonic


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events.get(key)
            if events is None:
                for stale_key in [item for item, values in self._events.items() if not values]:
                    del self._events[stale_key]
                if len(self._events) >= self.max_keys:
                    oldest_key = min(self._events, key=lambda item: self._events[item][-1])
                    del self._events[oldest_key]
                events = deque()
                self._events[key] = events
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
