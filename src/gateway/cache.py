"""Response caches.

* ``ExactCache`` — LRU + TTL keyed by a canonical hash of (model, messages, params).
* ``SemanticCache`` — cosine similarity over hashed character n-gram vectors
  (dependency-free; swap in a real embedding model in production). Only used for
  deterministic requests (temperature == 0); otherwise caching changes semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .resilience import Clock


def canonical_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class ExactCache:
    max_items: int = 10_000
    ttl: float = 3600.0
    clock: Clock = time.monotonic
    _d: OrderedDict = field(default_factory=OrderedDict)
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> Any | None:
        item = self._d.get(key)
        if item is None or self.clock() - item[0] > self.ttl:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return item[1]

    def put(self, key: str, value: Any) -> None:
        self._d[key] = (self.clock(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)


def embed(text: str, dim: int = 512, n: int = 3) -> list[float]:
    """Hashed character n-gram bag, L2-normalised."""
    t = f" {text.lower().strip()} "
    v = [0.0] * dim
    for i in range(len(t) - n + 1):
        h = int.from_bytes(hashlib.blake2b(t[i : i + n].encode(), digest_size=4).digest(), "big")
        v[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass
class SemanticCache:
    threshold: float = 0.92
    max_items: int = 2_000
    _items: list[tuple[str, list[float], Any]] = field(default_factory=list)
    hits: int = 0
    misses: int = 0

    def get(self, scope: str, text: str) -> tuple[Any, float] | None:
        q = embed(text)
        best, best_sim = None, -1.0
        for s, vec, val in self._items:
            if s != scope:
                continue
            sim = cosine(q, vec)
            if sim > best_sim:
                best, best_sim = val, sim
        if best is not None and best_sim >= self.threshold:
            self.hits += 1
            return best, best_sim
        self.misses += 1
        return None

    def put(self, scope: str, text: str, value: Any) -> None:
        self._items.append((scope, embed(text), value))
        if len(self._items) > self.max_items:
            self._items.pop(0)
