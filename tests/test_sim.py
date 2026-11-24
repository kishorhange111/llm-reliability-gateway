import asyncio

from gateway.sim import pct, run_strategy, to_markdown


def test_pct():
    assert pct([1, 2, 3, 4], 0.5) == 3
    assert pct([5], 0.99) == 5


def test_breaker_beats_plain_fallback_during_outage():
    kw = dict(rps=60, duration=2.5, outage=(0.5, 2.0), timeout=0.6, seed=0)
    single = asyncio.run(run_strategy("single", **kw))
    fb = asyncio.run(run_strategy("fallback", **kw))
    br = asyncio.run(run_strategy("fallback+breaker", **kw))
    assert single["availability_during_outage"] < 0.2
    assert fb["availability_during_outage"] > 0.9 and br["availability_during_outage"] > 0.9
    # the breaker stops paying the timeout on every request during the outage
    assert br["primary_calls"] < fb["primary_calls"]
    assert br["paid_timeout"] < fb["paid_timeout"]
    assert "| fallback+breaker |" in to_markdown([br])
