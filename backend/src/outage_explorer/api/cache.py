"""Query result cache: expires after a TTL, is cleared by every refresh, and is never shared across grants.

A result is stored under (grant key, build id, the SQL that ran). That makes three guarantees:
- it can never be served to a user who could not run the query: the query path authorizes every
  request before it looks here, and on top of that two roles share an entry only if their grants are
  identical (the grant key is the hash of every table and column they may see; Analyst and Admin do);
- it is never stale: a new model build has a new id, so old entries can't match even if the model was
  rebuilt outside the API (CLI); a refresh also clears the cache to free the memory at once;
- the key is the SQL regenerated from the parse tree, page included, so whitespace and keyword case
  don't matter, and each page is its own entry.

Memory is bounded by a total row budget, evicting the least recently used results first.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CacheKey = tuple[str, str, str]  # (grant_key, build_id, sql)


@dataclass
class _Entry:
    value: Any
    rows: int
    stored_at: float


class QueryCache:
    def __init__(self, ttl_seconds: float, max_rows: int, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_rows, self.clock = ttl_seconds, max_rows, clock
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._rows = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and self.max_rows > 0

    def get(self, key: CacheKey) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self.clock() - entry.stored_at >= self.ttl:
                self._drop(key)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def put(self, key: CacheKey, value: Any, rows: int) -> None:
        cost = max(rows, 1)
        if not self.enabled or cost > self.max_rows:
            return
        with self._lock:
            if key in self._entries:
                self._drop(key)
            self._entries[key] = _Entry(value, cost, self.clock())
            self._rows += cost
            while self._rows > self.max_rows:
                self._drop(next(iter(self._entries)))  # least recently used

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._rows = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _drop(self, key: CacheKey) -> None:
        self._rows -= self._entries.pop(key).rows
