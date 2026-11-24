| strategy | availability | during outage | p50 | p95 | p99 | p99 in outage | primary calls | requests that waited ≥ timeout | secondary share |
|---|---|---|---|---|---|---|---|---|---|
| fallback | 98.89% | 97.1% | 135 ms | 770 ms | 861 ms | 874 ms | 1156 | 341 | 37.2% |
| fallback | 99.25% | 98.1% | 146 ms | 731 ms | 849 ms | 864 ms | 1142 | 339 | 37.2% |
| fallback | 99.50% | 98.7% | 128 ms | 731 ms | 824 ms | 865 ms | 1120 | 321 | 35.1% |
| fallback+breaker | 98.77% | 97.4% | 109 ms | 418 ms | 598 ms | 618 ms | 520 | 128 | 48.0% |
| fallback+breaker | 98.50% | 97.1% | 113 ms | 433 ms | 597 ms | 729 ms | 510 | 104 | 46.6% |
| fallback+breaker | 98.99% | 97.7% | 102 ms | 419 ms | 584 ms | 783 ms | 513 | 116 | 46.8% |
