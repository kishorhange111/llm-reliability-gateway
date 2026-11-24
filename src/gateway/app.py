"""FastAPI front door with Prometheus metrics."""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from .providers import CompletionRequest
from .router import AllProvidersFailed, BudgetExceeded, Gateway, RateLimited


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.events = Counter(
            "gateway_events_total", "Gateway events", ["event", "label"], registry=self.registry
        )
        self.latency = Histogram(
            "gateway_request_seconds",
            "End-to-end request latency",
            ["outcome"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )

    def inc(self, name: str, **labels) -> None:
        self.events.labels(name, ",".join(f"{k}={v}" for k, v in sorted(labels.items()))).inc()


class Message(BaseModel):
    role: str
    content: str


class CompleteBody(BaseModel):
    messages: list[Message] = Field(min_length=1)
    max_tokens: int = Field(512, ge=1, le=64_000)
    temperature: float = Field(0.0, ge=0.0, le=1.0)


def create_app(gateway: Gateway, metrics: Metrics | None = None) -> FastAPI:
    metrics = metrics or Metrics()
    gateway.metrics = metrics
    app = FastAPI(title="llm-reliability-gateway")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "breakers": {k: b.state.value for k, b in gateway.breakers.items()}}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prom():
        return generate_latest(metrics.registry).decode()

    @app.post("/v1/complete")
    async def complete(body: CompleteBody, x_tenant_id: str = Header("default")):
        req = CompletionRequest(
            [m.model_dump() for m in body.messages], body.max_tokens, body.temperature
        )
        try:
            res = await gateway.handle(req, x_tenant_id)
        except RateLimited as e:
            metrics.latency.labels("rate_limited").observe(0)
            raise HTTPException(429, "rate limit exceeded", {"Retry-After": "1"}) from e
        except BudgetExceeded as e:
            raise HTTPException(402, "tenant budget exhausted") from e
        except AllProvidersFailed as e:
            metrics.latency.labels("failed").observe(0)
            raise HTTPException(502, f"all providers failed: {e}") from e
        metrics.latency.labels("ok").observe(res.latency)
        c = res.completion
        return {
            "text": c.text,
            "provider": c.provider,
            "cached": res.cached,
            "attempts": res.attempts,
            "usage": {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens},
            "cost_usd": c.cost_usd,
        }

    return app
