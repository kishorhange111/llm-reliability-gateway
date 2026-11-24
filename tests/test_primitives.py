import random

import pytest

from gateway.cache import ExactCache, SemanticCache, canonical_key
from gateway.resilience import (
    BreakerState,
    BudgetTracker,
    CircuitBreaker,
    TokenBucket,
    backoff_delays,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_full_lifecycle():
    c = FakeClock()
    br = CircuitBreaker(failure_threshold=3, window=10, cooldown=5, clock=c)
    for _ in range(2):
        br.record_failure()
    assert br.state == BreakerState.CLOSED and br.allow()
    br.record_failure()
    assert br.state == BreakerState.OPEN and not br.allow()
    c.t = 5.0
    assert br.allow() and br.state == BreakerState.HALF_OPEN
    assert not br.allow()  # only one probe in half-open
    br.record_failure()
    assert br.state == BreakerState.OPEN
    c.t = 10.0
    assert br.allow()
    br.record_success()
    assert br.state == BreakerState.CLOSED
    assert [s for _, s in br.transitions] == ["open", "half_open", "open", "half_open", "closed"]


def test_breaker_failures_outside_window_expire():
    c = FakeClock()
    br = CircuitBreaker(failure_threshold=3, window=10, clock=c)
    br.record_failure()
    br.record_failure()
    c.t = 11.0
    br.record_failure()
    assert br.state == BreakerState.CLOSED


def test_backoff_full_jitter_bounds():
    d = list(backoff_delays(6, base=0.1, cap=1.0, rng=random.Random(0)))
    caps = [0.1, 0.2, 0.4, 0.8, 1.0, 1.0]
    assert len(d) == 6 and all(0 <= x <= c for x, c in zip(d, caps, strict=True))


def test_token_bucket_refills():
    c = FakeClock()
    tb = TokenBucket(rate=2.0, capacity=2.0, clock=c)
    assert tb.try_acquire() and tb.try_acquire() and not tb.try_acquire()
    c.t = 0.5
    assert tb.try_acquire() and not tb.try_acquire()


def test_budget_tracker_window():
    c = FakeClock()
    b = BudgetTracker(limit_usd=1.0, window=60, clock=c)
    b.record(0.7)
    assert b.can_spend(0.3) and not b.can_spend(0.31)
    c.t = 61
    assert b.spent() == 0.0


def test_exact_cache_lru_and_ttl():
    c = FakeClock()
    cache = ExactCache(max_items=2, ttl=10, clock=c)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)  # evicts LRU "b"
    assert cache.get("b") is None and cache.get("c") == 3
    c.t = 11
    assert cache.get("a") is None


def test_canonical_key_ignores_dict_order():
    assert canonical_key({"a": 1, "b": [1, 2]}) == canonical_key({"b": [1, 2], "a": 1})


def test_semantic_cache_paraphrase_and_scope():
    sc = SemanticCache(threshold=0.8)
    sc.put("t1", "What is the refund policy for damaged items?", "answer")
    hit = sc.get("t1", "what is the refund policy for damaged items")
    assert hit is not None and hit[1] > 0.9
    assert sc.get("t2", "What is the refund policy for damaged items?") is None  # tenant isolation
    assert sc.get("t1", "How do I reset my password?") is None


@pytest.mark.parametrize("threshold", [0.5, 0.99])
def test_semantic_threshold_monotone(threshold):
    sc = SemanticCache(threshold=threshold)
    sc.put("s", "summarize the quarterly sales report", 1)
    hit = sc.get("s", "summarise the quarterly sales report")
    assert (hit is not None) == (threshold == 0.5)
