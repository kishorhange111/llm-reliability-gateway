import pytest
from fastapi.testclient import TestClient

from gateway.serve import build_app, providers_from_env, tenants_from_env


def test_env_parsing():
    assert [p.name for p in providers_from_env("mock, mock")] == ["mock0", "mock1"]
    t = tenants_from_env("acme:20:40:5;globex:5:10:1")
    assert t["acme"].rate == 20 and t["globex"].budget_usd == 1
    with pytest.raises(ValueError):
        providers_from_env("nope")


def test_built_app_serves(monkeypatch):
    monkeypatch.setenv("GATEWAY_TENANTS", "acme:100:100:10")
    c = TestClient(build_app())
    assert c.get("/healthz").json()["status"] == "ok"
    r = c.post(
        "/v1/complete",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"x-tenant-id": "acme"},
    )
    assert r.status_code == 200 and r.json()["provider"].startswith("mock")
    assert "gateway_request_seconds" in c.get("/metrics").text
