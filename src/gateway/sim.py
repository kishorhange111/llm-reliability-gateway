"""Failure-injection load simulation.

Open-loop Poisson arrivals (requests keep coming regardless of how slow the
gateway is — like real traffic) against two mock providers. The primary has a
*hanging* outage window in the middle of the run. Each reliability strategy is
replayed on the same arrival schedule and provider seeds.

    gateway-sim --rps 80 --duration 10 --out results
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
from pathlib import Path

from .providers import CompletionRequest, MockProvider
from .router import Gateway, Policy, TenantConfig

STRATEGIES = {
    "single": Policy(retries=0, fallback=False, use_breaker=False, exact_cache=False),
    "retry_x2": Policy(retries=2, fallback=False, use_breaker=False, exact_cache=False),
    "fallback": Policy(retries=1, fallback=True, use_breaker=False, exact_cache=False),
    # Breaker window sized to traffic: at 80 rps a 1 s window holds ~80 calls, enough
    # volume for a stable failure rate while still reacting within ~1 timeout.
    "fallback+breaker": Policy(
        retries=1,
        fallback=True,
        use_breaker=True,
        exact_cache=False,
        breaker_window=1.0,
        breaker_cooldown=2.0,
    ),
}


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def run_strategy(
    name: str, rps: float, duration: float, outage: tuple[float, float], timeout: float, seed: int
) -> dict:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    primary = MockProvider(
        "primary",
        median_latency=0.08,
        error_rate=0.02,
        usd_per_mtok_in=3,
        usd_per_mtok_out=15,
        outages=[(t0 + outage[0], t0 + outage[1])],
        seed=seed,
    )
    secondary = MockProvider(
        "secondary",
        median_latency=0.12,
        error_rate=0.01,
        usd_per_mtok_in=4,
        usd_per_mtok_out=20,
        seed=seed + 1,
    )
    pol = dataclasses.replace(STRATEGIES[name], timeout=timeout)
    gw = Gateway(
        [primary, secondary],
        pol,
        {"t": TenantConfig(rate=1e9, burst=1e9, budget_usd=1e9)},
        seed=seed,
    )
    rng = random.Random(seed)
    arrivals, t = [], 0.0
    while t < duration:
        t += rng.expovariate(rps)
        arrivals.append(t)

    results: list[tuple[float, bool, float, str | None]] = []

    async def one(i: int, at: float) -> None:
        await asyncio.sleep(max(0.0, t0 + at - loop.time()))
        start = loop.time()
        req = CompletionRequest([{"role": "user", "content": f"request {i}"}], 64, 0.7)
        try:
            r = await gw.handle(req, "t")
            results.append((at, True, loop.time() - start, r.completion.provider))
        except Exception:
            results.append((at, False, loop.time() - start, None))

    await asyncio.gather(*(one(i, a) for i, a in enumerate(arrivals)))
    ok = [r for r in results if r[1]]
    in_out = [r for r in results if outage[0] <= r[0] < outage[1]]
    lat_all = [r[2] for r in results]
    cost = sum(c for _, c in gw.budgets["t"]._spend)
    return {
        "strategy": name,
        "requests": len(results),
        "availability": len(ok) / len(results),
        "availability_during_outage": (sum(r[1] for r in in_out) / len(in_out)) if in_out else None,
        "p50_s": pct(lat_all, 0.50),
        "p95_s": pct(lat_all, 0.95),
        "p99_s": pct(lat_all, 0.99),
        "p95_during_outage_s": pct([r[2] for r in in_out], 0.95),
        "paid_timeout": sum(r[2] >= timeout for r in results),
        "p99_during_outage_s": pct([r[2] for r in in_out], 0.99),
        "primary_calls": primary.calls,
        "secondary_share": sum(r[3] == "secondary" for r in ok) / max(1, len(ok)),
        "cost_usd": cost,
        "breaker_transitions": [
            (round(ts - t0, 2), st) for ts, st in gw.breakers["primary"].transitions
        ],
    }


def to_markdown(rows: list[dict]) -> str:
    out = [
        "| strategy | availability | during outage | p50 | p95 | p99 | p99 in outage | "
        "primary calls | requests that waited ≥ timeout | secondary share |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['strategy']} | {100 * r['availability']:.2f}% | "
            f"{100 * (r['availability_during_outage'] or 0):.1f}% | {1000 * r['p50_s']:.0f} ms | "
            f"{1000 * r['p95_s']:.0f} ms | {1000 * r['p99_s']:.0f} ms | "
            f"{1000 * r['p99_during_outage_s']:.0f} ms | {r['primary_calls']} | "
            f"{r['paid_timeout']} | "
            f"{100 * r['secondary_share']:.1f}% |"
        )
    return "\n".join(out)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="gateway-sim")
    p.add_argument("--rps", type=float, default=80)
    p.add_argument("--duration", type=float, default=10)
    p.add_argument("--outage", type=float, nargs=2, default=[3.0, 7.0])
    p.add_argument("--timeout", type=float, default=1.0)
    p.add_argument("--seeds", type=int, nargs="+", default=[0])
    p.add_argument("--strategies", nargs="+", default=list(STRATEGIES))
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args(argv)
    rows = []
    for s in a.strategies:
        for seed in a.seeds:
            r = asyncio.run(run_strategy(s, a.rps, a.duration, tuple(a.outage), a.timeout, seed))
            r["seed"] = seed
            rows.append(r)
            print(
                f"{s} seed={seed}: availability={r['availability']:.4f} p99={r['p99_s']:.3f}s",
                flush=True,
            )
    md = to_markdown(rows)
    print(md)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "sim.json").write_text(json.dumps(rows, indent=1))
        (a.out / "sim.md").write_text(md + "\n")


if __name__ == "__main__":
    main()
