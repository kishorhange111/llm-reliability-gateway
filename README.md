# llm-reliability-gateway

A production-style LLM gateway with multi-provider routing, fallback, circuit breakers, retries with full-jitter backoff, per-tenant rate limits and budgets, exact and semantic caching, and Prometheus metrics. It also ships a **failure-injection load simulator**, which measures what each reliability mechanism buys.

The providers here are mocks with configurable latency, error rates and outage windows, so every number below is **simulated**, not measured against a real API. The value is the method: an open-loop load test with failure injection. The ranking of the strategies is what transfers to a real deployment; the absolute milliseconds don't.

## Components

| Module | What it does |
|---|---|
| `resilience.py` | `CircuitBreaker` (failure-*rate* over a sliding time window, with minimum volume and half-open probes), `backoff_delays` (full jitter), `TokenBucket`, `BudgetTracker` |
| `cache.py` | Exact cache (only for `temperature == 0`, with TTL) and a semantic cache (embedding cosine threshold) |
| `providers.py` | `Provider` protocol, `MockProvider` (latency distribution, error rate, hanging outages), Anthropic adapter |
| `router.py` | `Gateway.handle`: tenant rate limit → budget check → cache → providers in priority order, each with a breaker, per-attempt timeout and retries |
| `app.py` | FastAPI app with `/v1/complete`, `/healthz` and `/metrics` (Prometheus) |
| `sim.py` | Open-loop Poisson load against two mock providers, with a primary outage window |

## Simulation: what does each mechanism buy?

The setup:
- Poisson arrivals at 80 rps for 10 s, about 800 requests. The load is open-loop: arrivals don't wait on the gateway.
- The primary provider has an 80 ms median latency and 2% errors. Between t = 3 and 7 s its calls **hang**, the worst kind of outage because nothing fails fast.
- The secondary provider has a 120 ms median latency and 1% errors.
- The per-attempt timeout is 1.0 s.
- Each strategy is replayed with 3 seeds.

```
gateway-sim --rps 80 --duration 10 --outage 3 7 --timeout 1.0 --seeds 0 1 2 --out results
```

Mean over 3 seeds; the per-seed rows are in [`results/sim.md`](results/sim.md):

| strategy | availability | p95 | p99 | primary calls | requests that waited ≥ 1 s |
|---|---|---|---|---|---|
| single provider, no retry | 60.6% | 1.02 s | 1.02 s | 802 | 307 |
| retry ×2 | 80.8% | 3.08 s | 3.09 s | 1347 | 307 |
| fallback to secondary | **100%** | 2.23 s | 2.37 s | 1121 | 307 |
| fallback + circuit breaker | **100%** | **1.18 s** | 2.19 s | **408** | **95** |

What the table shows:
- **Retries alone are the wrong tool for a hanging outage.** They lift availability to about 81% only because some retries land after the outage ends, and they triple tail latency.
- **Fallback buys availability; the breaker buys latency and cost.** Without the breaker, every request in the outage first pays the full timeout on the primary. With the breaker, about 70% fewer requests wait out a timeout, there are 64% fewer primary calls, and p95 halves.
- **p99 is bounded by detection latency.** A breaker can only open after failures *complete*, and for a hang that takes at least one timeout. The roughly 80 requests already in flight in that first second set the p99. To bring it down you need shorter timeouts or hedged requests, not a better breaker.

### Tight timeout (0.2 s)

```
gateway-sim --timeout 0.2 --strategies fallback fallback+breaker --seeds 0 1 2 --out results/tight_timeout
```

Both strategies reach about 99% availability. The breaker cuts p99 from about 850 ms to about 590 ms. The remaining failures come from the secondary's own latency tail exceeding 0.2 s. See [`results/tight_timeout/sim.md`](results/tight_timeout/sim.md).

### A bug the simulation caught

The first breaker used a **count** rule: open after N failures within T seconds. Under load that rule is wrong. Once the secondary carried all 80 rps during the outage, its 1% background errors were enough to reach N failures, so the *secondary's* breaker opened too. Every request then failed with `primary:breaker_open; secondary:breaker_open`. Whether that happened depended on the seed: one seed showed 60% availability and the other two 100%. With a tight timeout, the secondary's slow tail made the same thing happen every time, and availability collapsed to about 15%: the breakers caused the outage themselves.

The fix was a **failure-rate** breaker in the style of Resilience4j: open only when the failure fraction over a sliding window is at least 50% with a minimum of 20 calls. The window must be sized to the traffic: with a 10 s window, the 3 s of healthy calls before the outage diluted the rate and the breaker took about 3 s to open; a 1 s window opens in about 1.2 s. Regression tests cover both the noise case and window expiry (`tests/test_primitives.py`).

## Deploy

| artifact | what |
|---|---|
| `src/gateway/serve.py` | env-configured entrypoint: providers in priority order (`GATEWAY_PROVIDERS=anthropic,mock`), timeout, retries, and tenants as `name:rate:burst:budget_usd` |
| `Dockerfile` | multi-stage build, non-root (UID 10001), `HEALTHCHECK` on `/healthz` |
| `docker-compose.yml` | gateway + Prometheus scraping `/metrics`; read-only container filesystem |
| `deploy/k8s/gateway.yaml` | Deployment (readiness/liveness probes, requests/limits, read-only root FS, dropped capabilities, seccomp, API key from a Secret), Service, HPA (2–10 replicas on CPU), PodDisruptionBudget |

```
docker compose up --build        # gateway on :8080, Prometheus on :9090
curl -X POST localhost:8080/v1/complete -H 'x-tenant-id: acme' -H 'content-type: application/json'      -d '{"messages":[{"role":"user","content":"hi"}]}'
```

CI builds the image, starts it, checks that it runs as non-root and smoke-tests `/healthz` and `/v1/complete`.

### AWS (Terraform, ECS Fargate)

`deploy/terraform/aws-ecs` deploys the same image into an **existing** VPC. In a customer account the network usually belongs to another team, so the VPC and subnets are inputs:

- **ECR** with immutable tags, scan on push and a lifecycle rule;
- **Secrets Manager** holds `ANTHROPIC_API_KEY`. The value is set out of band, so it never enters Terraform state, and only the execution role can read it;
- **Fargate service** with a read-only root filesystem, UID 10001 and private subnets. The deployment circuit breaker auto-rolls back bad images;
- **ALB**, internal by default. It uses TLS 1.3 when a certificate is given. The idle timeout is sized to the gateway's retry budget, because the ALB's default 60 s cuts long completions;
- **Security groups:** only the load balancer may reach the tasks, and tasks may only make outbound HTTPS calls;
- **Autoscaling** targets **requests per task**, with CPU as a backstop. The gateway is I/O-bound (it mostly waits on the model), so CPU alone under-scales;
- **Alarms:**
  - 5xx, which means every provider in the fallback chain failed;
  - p99 above the per-attempt timeout, which means requests are paying for timeouts and the breaker isn't opening;
  - unhealthy hosts.

```
cd deploy/terraform/aws-ecs
cp terraform.tfvars.example terraform.tfvars   # fill in VPC / subnets / cert
terraform init && terraform plan
```

CI runs `terraform fmt -check` and `terraform validate`. Nothing here has been applied to a real account.

## Development

```
pip install -e ".[dev]"
pytest -q
ruff check .
```
