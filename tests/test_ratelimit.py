"""The rate limiter must bound its own memory.

The version this replaced was a `defaultdict(lambda: deque(maxlen=20))`: it
capped each bucket's length but never removed a bucket, so one entry per
distinct client IP accumulated for the process's lifetime. A scanner walking
the address space would grow it until the container was OOM-killed, turning
the rate limiter into the outage it exists to prevent.
"""
import time

from app.services.ratelimit import SlidingWindowLimiter


def test_allows_up_to_the_limit_then_rejects():
    lim = SlidingWindowLimiter()
    assert [lim.hit("ip", 3, 60) for _ in range(5)] == [False, False, False, True, True]


def test_separate_keys_do_not_share_a_budget():
    lim = SlidingWindowLimiter()
    for _ in range(3):
        lim.hit("a", 3, 60)
    assert lim.hit("a", 3, 60) is True
    assert lim.hit("b", 3, 60) is False


def test_window_expiry_lets_a_client_back_in():
    lim = SlidingWindowLimiter()
    assert lim.hit("ip", 1, 0.05) is False
    assert lim.hit("ip", 1, 0.05) is True
    time.sleep(0.06)
    assert lim.hit("ip", 1, 0.05) is False


def test_a_blocked_attempt_does_not_extend_the_lockout():
    """A client hammering while blocked must not keep pushing its own window
    forward - otherwise the penalty grows without bound under load."""
    lim = SlidingWindowLimiter()
    assert lim.hit("ip", 1, 0.05) is False
    for _ in range(20):
        assert lim.hit("ip", 1, 0.05) is True
    time.sleep(0.06)
    assert lim.hit("ip", 1, 0.05) is False


def test_expired_buckets_are_swept_instead_of_accumulating():
    """The leak: 5000 one-shot IPs must not leave 5000 live buckets."""
    lim = SlidingWindowLimiter(sweep_every=64)
    for i in range(5000):
        lim.hit(f"ip-{i}", 5, 0.01)
        if i % 500 == 0:
            time.sleep(0.011)  # let earlier windows age out
    assert len(lim) < 5000, f"no eviction happened: {len(lim)} buckets retained"


def test_key_count_stays_under_the_hard_cap_even_without_expiry():
    """Worst case - an attacker cycling IPs faster than anything expires.
    Capacity eviction, not expiry, is what has to hold the line here."""
    lim = SlidingWindowLimiter(max_keys=100, sweep_every=32)
    for i in range(3000):
        lim.hit(f"ip-{i}", 5, 3600)  # window far longer than the test runs
    assert len(lim) <= 100, f"unbounded growth: {len(lim)} buckets"


def test_eviction_prefers_the_least_recently_used_key():
    """A sustained abuser stays tracked; one-shot scanners are what fall out."""
    lim = SlidingWindowLimiter(max_keys=50, sweep_every=16)
    for i in range(500):
        lim.hit("persistent", 10_000, 3600)  # keeps touching itself
        lim.hit(f"drive-by-{i}", 5, 3600)
    assert "persistent" in lim._buckets


def test_clear_resets_everything():
    """conftest relies on this between tests - TestClient reuses one fake IP."""
    lim = SlidingWindowLimiter()
    for _ in range(3):
        lim.hit("ip", 3, 60)
    assert lim.hit("ip", 3, 60) is True
    lim.clear()
    assert len(lim) == 0
    assert lim.hit("ip", 3, 60) is False
