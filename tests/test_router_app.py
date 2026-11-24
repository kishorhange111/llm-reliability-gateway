import asyncio

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.providers import Completion, CompletionRequest, MockProvider, ProviderError
from gateway.router import (
    AllProvidersFailed,
    BudgetExceeded,
    Gateway,
    Policy,
    RateLimited,
    TenantConfig,
)


def req(text="hi", temperature=0.0):
    return CompletionRequest([{"role": "user", "content": text}], 32, temperature)


def run(coro):
    return asyncio.run(coro)


class Scripted:
    """Provider that fails a fixed number of times, then succeeds."""

    def __init__(self, name, failures=0, retryable=True, hang=False):
        self.name, self.failures, self.retryable, self.hang = name, failures, retryable, hang
        self.usd_per_mtok_in = self.usd_per_mtok_out = 1.0
        self.calls = 0

    async def complete(self, r):
        self.calls += 1
        if self.hang:
            await asyncio.sleep(10)
        if self.calls <= self.failures:
            raise ProviderError("503", self.retryable)
        return Completion("ok", self.name, 1, 1, 0.001)


def test_retry_then_success_on_primary():
    a = Scripted("a", failures=1)
    g = Gateway([a, Scripted("b")], Policy(retries=1, backoff_base=0.001))
    res = run(g.handle(req()))
    assert res.completion.provider == "a" and res.attempts == ["a:error", "a:ok"]


def test_fallback_to_secondary():
    g = Gateway([Scripted("a", failures=99), Scripted("b")], Policy(retries=0))
    res = run(g.handle(req()))
    assert res.completion.provider == "b" and res.attempts == ["a:error", "b:ok"]


def test_timeout_counts_as_failure_and_falls_back():
    g = Gateway([Scripted("a", hang=True), Scripted("b")], Policy(timeout=0.05, retries=0))
    res = run(g.handle(req()))
    assert res.attempts == ["a:timeout", "b:ok"]


def test_non_retryable_error_does_not_fall_back():
    b = Scripted("b")
    g = Gateway([Scripted("a", failures=1, retryable=False), b], Policy(retries=2))
    with pytest.raises(AllProvidersFailed):
        run(g.handle(req()))
    assert b.calls == 0


def test_no_fallback_policy_fails():
    g = Gateway([Scripted("a", failures=99), Scripted("b")], Policy(retries=0, fallback=False))
    with pytest.raises(AllProvidersFailed):
        run(g.handle(req()))


def test_open_breaker_skips_provider():
    a = Scripted("a", failures=99)
    g = Gateway([a, Scripted("b")], Policy(retries=0))
    for _ in range(5):
        run(g.handle(req(f"q{_}")))
    calls = a.calls
    res = run(g.handle(req("new")))
    assert a.calls == calls and res.attempts[0] == "a:breaker_open"


def test_exact_cache_only_for_deterministic_requests():
    a = Scripted("a")
    g = Gateway([a])
    run(g.handle(req("same")))
    r2 = run(g.handle(req("same")))
    assert r2.cached == "exact" and a.calls == 1
    run(g.handle(req("same", temperature=0.7)))
    run(g.handle(req("same", temperature=0.7)))
    assert a.calls == 3


def test_cache_is_tenant_scoped():
    a = Scripted("a")
    g = Gateway([a])
    run(g.handle(req("x"), "t1"))
    assert run(g.handle(req("x"), "t2")).cached is None


def test_semantic_cache_hit():
    g = Gateway([Scripted("a")], Policy(semantic_cache=True))
    run(g.handle(req("What is the refund policy for damaged items?")))
    r = run(g.handle(req("what is the refund policy for damaged items")))
    assert r.cached == "semantic"


def test_rate_limit_and_budget():
    g = Gateway([Scripted("a")], tenants={"t": TenantConfig(rate=0.0, burst=1.0)})
    run(g.handle(req("1"), "t"))
    with pytest.raises(RateLimited):
        run(g.handle(req("2"), "t"))
    g2 = Gateway([Scripted("a")], tenants={"t": TenantConfig(budget_usd=0.0005)})
    run(g2.handle(req("1"), "t"))
    with pytest.raises(BudgetExceeded):
        run(g2.handle(req("2"), "t"))


def test_mock_provider_costs_and_outage():
    async def go():
        loop = asyncio.get_running_loop()
        p = MockProvider(
            "m", median_latency=0.001, outages=[(loop.time(), loop.time() + 5)], hang_seconds=0.01
        )
        with pytest.raises(ProviderError):
            await p.complete(req())
        p.outages = []
        c = await p.complete(req("x" * 400))
        assert c.input_tokens == 100 and c.cost_usd > 0

    run(go())


def test_http_api_and_metrics():
    g = Gateway([Scripted("a", failures=99), Scripted("b")], Policy(retries=0))
    client = TestClient(create_app(g))
    r = client.post(
        "/v1/complete",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Tenant-Id": "acme"},
    )
    assert r.status_code == 200 and r.json()["provider"] == "b"
    assert client.post("/v1/complete", json={"messages": []}).status_code == 422
    m = client.get("/metrics").text
    assert "gateway_events_total" in m and "upstream_failure" in m
    assert client.get("/healthz").json()["breakers"]["a"] == "closed"


def test_http_502_when_everything_fails():
    g = Gateway([Scripted("a", failures=99)], Policy(retries=0))
    client = TestClient(create_app(g))
    r = client.post("/v1/complete", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
