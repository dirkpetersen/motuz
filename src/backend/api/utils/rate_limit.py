"""
In-memory sliding-window rate limits for the worker endpoints (views/worker_views.py).

The app runs as a single uWSGI process with threads (wsgi.ini), so one process-wide
table is enough; with several processes each would count separately (the limits are
per process then). Keys are bounded: old keys are dropped when the table grows.
"""
import collections
import threading
import time


class RateLimiter:
    def __init__(self, limit, window_seconds, max_keys=10000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits = collections.OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        """Records one request for `key`; False if it exceeds the limit"""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = collections.deque()
                if len(self._hits) > self.max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()
