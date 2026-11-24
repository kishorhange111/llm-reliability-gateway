"""Provider interface plus a fault-injecting mock and a Claude provider."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CompletionRequest:
    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.0
    model_hint: str | None = None

    def prompt_text(self) -> str:
        return "\n".join(str(m.get("content", "")) for m in self.messages)


@dataclass
class Completion:
    text: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class Provider(Protocol):
    name: str
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    async def complete(self, req: CompletionRequest) -> Completion: ...


@dataclass
class MockProvider:
    """Latency ~ lognormal; failures are 5xx (fast), timeouts (hang), or 4xx (non-retryable).

    ``outages`` is a list of (start, end) times on the event-loop clock during which
    the provider hangs — the failure mode that makes circuit breakers matter.
    """

    name: str
    median_latency: float = 0.05
    latency_sigma: float = 0.5
    error_rate: float = 0.0
    bad_request_rate: float = 0.0
    usd_per_mtok_in: float = 3.0
    usd_per_mtok_out: float = 15.0
    outages: list[tuple[float, float]] = field(default_factory=list)
    hang_seconds: float = 30.0
    seed: int = 0
    calls: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _in_outage(self) -> bool:
        now = asyncio.get_running_loop().time()
        return any(s <= now < e for s, e in self.outages)

    async def complete(self, req: CompletionRequest) -> Completion:
        self.calls += 1
        if self._in_outage():
            await asyncio.sleep(self.hang_seconds)
            raise ProviderError(f"{self.name}: upstream hung")
        await asyncio.sleep(self.median_latency * self._rng.lognormvariate(0, self.latency_sigma))
        u = self._rng.random()
        if u < self.bad_request_rate:
            raise ProviderError(f"{self.name}: 400 bad request", retryable=False)
        if u < self.bad_request_rate + self.error_rate:
            raise ProviderError(f"{self.name}: 503 overloaded")
        tin = max(1, len(req.prompt_text()) // 4)
        tout = min(req.max_tokens, 64)
        cost = (tin * self.usd_per_mtok_in + tout * self.usd_per_mtok_out) / 1e6
        return Completion(f"[{self.name}] ok", self.name, tin, tout, cost)


class AnthropicProvider:
    """Claude through the official async SDK. SDK-level retries are disabled so the
    gateway's own retry/fallback policy is the single source of truth."""

    def __init__(self, model: str = "claude-opus-5-5", name: str = "anthropic"):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=0)
        self.model = model
        self.name = name
        self.usd_per_mtok_in, self.usd_per_mtok_out = 4.0, 20.0

    async def complete(self, req: CompletionRequest) -> Completion:
        a = self._anthropic
        try:
            resp = await self.client.messages.create(
                model=self.model, max_tokens=req.max_tokens, messages=req.messages
            )
        except a.RateLimitError as e:
            raise ProviderError(f"{self.name}: 429") from e
        except a.APIStatusError as e:
            raise ProviderError(
                f"{self.name}: {e.status_code}", retryable=e.status_code >= 500
            ) from e
        except a.APIConnectionError as e:
            raise ProviderError(f"{self.name}: connection error") from e
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        cost = (
            u.input_tokens * self.usd_per_mtok_in + u.output_tokens * self.usd_per_mtok_out
        ) / 1e6
        return Completion(text, self.name, u.input_tokens, u.output_tokens, cost)
