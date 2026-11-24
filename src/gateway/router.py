"""The request path: admission -> cache -> ordered providers with timeout/retry/breaker."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from .cache import ExactCache, SemanticCache, canonical_key
from .providers import Completion, CompletionRequest, Provider, ProviderError
from .resilience import BudgetTracker, CircuitBreaker, TokenBucket, backoff_delays


class RateLimited(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class AllProvidersFailed(Exception):
    pass


@dataclass
class Policy:
    timeout: float = 2.0  # per-attempt deadline
    retries: int = 1  # extra attempts per provider for retryable errors
    fallback: bool = True
    use_breaker: bool = True
    backoff_base: float = 0.02
    exact_cache: bool = True
    semantic_cache: bool = False


@dataclass
class Result:
    completion: Completion
    cached: str | None = None  # "exact" | "semantic" | None
    attempts: list[str] = field(default_factory=list)
    latency: float = 0.0


@dataclass
class TenantConfig:
    rate: float = 50.0
    burst: float = 100.0
    budget_usd: float = 100.0


class Gateway:
    def __init__(
        self,
        providers: list[Provider],
        policy: Policy | None = None,
        tenants: dict[str, TenantConfig] | None = None,
        seed: int = 0,
        clock=time.monotonic,
        metrics=None,
    ):
        self.providers = providers
        self.policy = policy or Policy()
        self.breakers = {p.name: CircuitBreaker(clock=clock) for p in providers}
        self.tenant_cfg = tenants or {}
        self.buckets: dict[str, TokenBucket] = {}
        self.budgets: dict[str, BudgetTracker] = {}
        self.exact = ExactCache(clock=clock)
        self.semantic = SemanticCache()
        self.rng = random.Random(seed)
        self.clock = clock
        self.metrics = metrics

    def _tenant(self, tenant: str) -> tuple[TokenBucket, BudgetTracker]:
        if tenant not in self.buckets:
            cfg = self.tenant_cfg.get(tenant, TenantConfig())
            self.buckets[tenant] = TokenBucket(cfg.rate, cfg.burst, clock=self.clock)
            self.budgets[tenant] = BudgetTracker(cfg.budget_usd, clock=self.clock)
        return self.buckets[tenant], self.budgets[tenant]

    def _count(self, name: str, **labels) -> None:
        if self.metrics is not None:
            self.metrics.inc(name, **labels)

    async def handle(self, req: CompletionRequest, tenant: str = "default") -> Result:
        t0 = asyncio.get_running_loop().time()
        bucket, budget = self._tenant(tenant)
        if not bucket.try_acquire():
            self._count("rejected", reason="rate_limit", tenant=tenant)
            raise RateLimited(tenant)
        if not budget.can_spend():
            self._count("rejected", reason="budget", tenant=tenant)
            raise BudgetExceeded(tenant)

        pol = self.policy
        deterministic = req.temperature == 0.0
        key = canonical_key(
            {"t": tenant, "m": req.messages, "mx": req.max_tokens, "h": req.model_hint}
        )
        if pol.exact_cache and deterministic and (hit := self.exact.get(key)) is not None:
            self._count("cache_hit", kind="exact")
            return Result(hit, "exact", latency=asyncio.get_running_loop().time() - t0)
        if pol.semantic_cache and deterministic:
            if (s := self.semantic.get(tenant, req.prompt_text())) is not None:
                self._count("cache_hit", kind="semantic")
                return Result(s[0], "semantic", latency=asyncio.get_running_loop().time() - t0)

        attempts: list[str] = []
        order = self.providers if pol.fallback else self.providers[:1]
        for p in order:
            br = self.breakers[p.name]
            if pol.use_breaker and not br.allow():
                attempts.append(f"{p.name}:breaker_open")
                continue
            delays = backoff_delays(pol.retries, base=pol.backoff_base, rng=self.rng)
            for attempt in range(pol.retries + 1):
                try:
                    comp = await asyncio.wait_for(p.complete(req), pol.timeout)
                except (ProviderError, asyncio.TimeoutError) as e:
                    retryable = getattr(e, "retryable", True)
                    kind = "timeout" if isinstance(e, asyncio.TimeoutError) else "error"
                    attempts.append(f"{p.name}:{kind}")
                    self._count("upstream_failure", provider=p.name, kind=kind)
                    if pol.use_breaker:
                        br.record_failure()
                    if not retryable:
                        raise AllProvidersFailed(f"non-retryable: {e}") from e
                    if attempt < pol.retries and (not pol.use_breaker or br.allow()):
                        await asyncio.sleep(next(delays))
                        continue
                    break
                else:
                    if pol.use_breaker:
                        br.record_success()
                    attempts.append(f"{p.name}:ok")
                    budget.record(comp.cost_usd)
                    if deterministic:
                        self.exact.put(key, comp)
                        if pol.semantic_cache:
                            self.semantic.put(tenant, req.prompt_text(), comp)
                    self._count("success", provider=p.name)
                    lat = asyncio.get_running_loop().time() - t0
                    return Result(comp, None, attempts, lat)
        self._count("failed")
        raise AllProvidersFailed("; ".join(attempts))
