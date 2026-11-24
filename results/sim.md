| strategy | availability | during outage | p50 | p95 | p99 | p99 in outage | primary calls | requests that waited ≥ timeout | secondary share |
|---|---|---|---|---|---|---|---|---|---|
| single | 60.71% | 0.0% | 133 ms | 1017 ms | 1023 ms | 1025 ms | 812 | 310 | 0.0% |
| single | 59.65% | 0.3% | 135 ms | 1016 ms | 1017 ms | 1017 ms | 798 | 312 | 0.0% |
| single | 61.56% | 0.3% | 122 ms | 1016 ms | 1019 ms | 1020 ms | 796 | 300 | 0.0% |
| retry_x2 | 80.42% | 48.4% | 137 ms | 3080 ms | 3101 ms | 3107 ms | 1363 | 308 | 0.0% |
| retry_x2 | 79.82% | 48.7% | 133 ms | 3078 ms | 3090 ms | 3098 ms | 1354 | 312 | 0.0% |
| retry_x2 | 82.16% | 52.8% | 133 ms | 3071 ms | 3091 ms | 3103 ms | 1323 | 300 | 0.0% |
| fallback | 100.00% | 100.0% | 131 ms | 2232 ms | 2367 ms | 2417 ms | 1135 | 308 | 29.3% |
| fallback | 100.00% | 100.0% | 133 ms | 2232 ms | 2351 ms | 2382 ms | 1120 | 312 | 29.7% |
| fallback | 100.00% | 100.0% | 133 ms | 2235 ms | 2383 ms | 2447 ms | 1107 | 301 | 27.9% |
| fallback+breaker | 100.00% | 100.0% | 132 ms | 1204 ms | 2194 ms | 2267 ms | 409 | 102 | 65.5% |
| fallback+breaker | 100.00% | 100.0% | 120 ms | 1168 ms | 2198 ms | 2249 ms | 416 | 96 | 63.0% |
| fallback+breaker | 100.00% | 100.0% | 118 ms | 1155 ms | 2181 ms | 2207 ms | 400 | 86 | 63.7% |
