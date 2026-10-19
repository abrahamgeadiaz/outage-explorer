"""Login rate limiting: failed attempts in a sliding window, per (client, username) and per client.

- After LOGIN_MAX_FAILURES failed logins for one username from one client within the window, that
  pair is refused (429 + Retry-After) until the oldest failure leaves the window. The password is not
  even checked while blocked, so guessing gains nothing, and a correct password is refused too.
- After LOGIN_MAX_FAILURES_PER_CLIENT failures from one client across all usernames, the client is
  refused: this stops trying one password against many usernames.
- A successful login clears that pair's failures.

Deliberately not a per-username lock across all clients: anyone could then lock a real user out by
failing on purpose. The client is the connection's address; X-Forwarded-For is not trusted (a client
can set it), so behind a reverse proxy this needs the proxy's real-IP setting.

In memory, per process; the number of tracked keys is capped so a flood of usernames can't grow it.
"""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000


class LoginLimiter:
    def __init__(
        self,
        max_failures: int,
        max_failures_per_client: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures, self.max_per_client = max_failures, max_failures_per_client
        self.window, self.clock = window_seconds, clock
        self._failures: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, client: str, username: str) -> int | None:
        """Seconds until this attempt would be allowed, or None if it is allowed now."""
        now = self.clock()
        checks = ((_pair(client, username), self.max_failures), ((client, ""), self.max_per_client))
        with self._lock:
            waits = [self._wait(key, limit, now) for key, limit in checks if limit > 0]
        blocked = [w for w in waits if w is not None]
        return max(blocked) if blocked else None

    def failed(self, client: str, username: str) -> None:
        now = self.clock()
        with self._lock:
            for key in (_pair(client, username), (client, "")):
                self._failures.setdefault(key, deque()).append(now)
                self._failures.move_to_end(key)
            while len(self._failures) > MAX_TRACKED_KEYS:
                self._failures.popitem(last=False)  # least recently failed

    def succeeded(self, client: str, username: str) -> None:
        with self._lock:
            self._failures.pop(_pair(client, username), None)

    def _wait(self, key: tuple[str, str], limit: int, now: float) -> int | None:
        times = self._failures.get(key)
        if not times:
            return None
        while times and now - times[0] >= self.window:
            times.popleft()
        if len(times) < limit:
            return None
        # Allowed again once enough failures have left the window to go below the limit.
        return max(1, math.ceil(times[len(times) - limit] + self.window - now))


def _pair(client: str, username: str) -> tuple[str, str]:
    return client, "user:" + username.strip().lower()
