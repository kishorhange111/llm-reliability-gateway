"""Process entrypoint: build the gateway from environment variables.

GATEWAY_PROVIDERS   comma list, in priority order: "anthropic", "mock" (default "mock,mock")
GATEWAY_TIMEOUT_S   per-attempt timeout (default 30)
GATEWAY_RETRIES     extra attempts per provider (default 1)
GATEWAY_TENANTS     "name:rate:burst:budget_usd;..." (default one permissive tenant)

uvicorn gateway.serve:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os

from .app import create_app
from .providers import MockProvider
from .router import Gateway, Policy, TenantConfig


def providers_from_env(spec: str) -> list:
    out = []
    for i, name in enumerate(p.strip() for p in spec.split(",") if p.strip()):
        if name == "anthropic":
            from .providers import AnthropicProvider

            out.append(AnthropicProvider(model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")))
        elif name == "mock":
            out.append(MockProvider(f"mock{i}", median_latency=0.05, error_rate=0.01, seed=i))
        else:
            raise ValueError(f"unknown provider {name!r}")
    return out


def tenants_from_env(spec: str | None) -> dict[str, TenantConfig]:
    if not spec:
        return {"default": TenantConfig()}
    out = {}
    for item in spec.split(";"):
        name, rate, burst, budget = item.split(":")
        out[name] = TenantConfig(float(rate), float(burst), float(budget))
    return out


def build_app():
    gw = Gateway(
        providers_from_env(os.getenv("GATEWAY_PROVIDERS", "mock,mock")),
        Policy(
            timeout=float(os.getenv("GATEWAY_TIMEOUT_S", "30")),
            retries=int(os.getenv("GATEWAY_RETRIES", "1")),
        ),
        tenants_from_env(os.getenv("GATEWAY_TENANTS")),
    )
    return create_app(gw)


app = build_app()
