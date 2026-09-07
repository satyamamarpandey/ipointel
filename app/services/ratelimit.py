from __future__ import annotations
"""Bounded in-memory sliding-window rate limiter.

Replaces a plain `defaultdict(lambda: deque(maxlen=20))`, which had no
eviction at all: every distinct client IP created a bucket that lived for the
process's whole lifetime. A crawler, a botnet or a port scanner walking the
address space would grow that dict without limit until the container was
OOM-killed - the rate limiter itself becoming the denial-of-service vector it
exists to prevent.

Two independent bounds keep it flat under that load:
  * expiry - a bucket whose newest hit has aged out of the window carries no
    information and is dropped during an amortised sweep;
  * capacity - a hard ceiling on tracked keys, evicting least-recently-used
    first, so even an attacker cycling IPs faster than the sweep cannot grow
    memory without limit.

Evicting a key is deliberately fail-open (the next request from that IP starts
a fresh window). Under key-cycling pressure the evicted entries are precisely
the least recently seen, so a sustained abuser stays tracked while one-shot
scanners are what fall out.

Deliberately process-local: it needs no Redis and adds no dependency, but it
therefore does not coordinate across workers. Run one web process per limit
budget, or move to a shared store first - see docs/DEPLOYMENT.md.
"""
import time
from collections import OrderedDict, deque

DEFAULT_MAX_KEYS = 20_000
_SWEEP_EVERY = 512


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = DEFAULT_MAX_KEYS, sweep_every: int = _SWEEP_EVERY):
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys
        self._sweep_every = sweep_every
        self._widest_window = 0.0
        self._ops = 0

    def hit(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        """Record an attempt against `key`. True means "reject this request".

        A rejected attempt is not appended, matching the previous behaviour:
        a client already at the limit cannot push its own window forward and
        extend its own lockout.
        """
        now = time.monotonic()
        self._widest_window = max(self._widest_window, window_s)
        self._ops += 1
        if self._ops % self._sweep_every == 0:
            self._sweep(now)

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)  # least-recently-used ordering

        cutoff = now - window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            return True
        bucket.append(now)

        if len(self._buckets) > self._max_keys:
            self._sweep(now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        return False

    def _sweep(self, now: float) -> int:
        """Drop buckets that can no longer affect any decision. Returns the
        number removed (used by the tests to prove eviction actually runs)."""
        cutoff = now - self._widest_window
        stale = [k for k, b in self._buckets.items() if not b or b[-1] <= cutoff]
        for k in stale:
            del self._buckets[k]
        return len(stale)

    def clear(self) -> None:
        self._buckets.clear()
        self._widest_window = 0.0
        self._ops = 0

    def __len__(self) -> int:
        return len(self._buckets)
