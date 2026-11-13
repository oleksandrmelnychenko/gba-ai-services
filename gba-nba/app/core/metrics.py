"""In-process metrics counters (thread-safe). Exposed via /metrics.

Kept dependency-free; if Prometheus is wanted later, swap the backend here only.
"""
from __future__ import annotations

import threading
import time


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.requests = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_latency_ms = 0.0

    def record_request(self, latency_ms: float, error: bool = False) -> None:
        with self._lock:
            self.requests += 1
            self.total_latency_ms += latency_ms
            if error:
                self.errors += 1

    def record_cache(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.cache_hits += 1
            else:
                self.cache_misses += 1

    def snapshot(self) -> dict:
        with self._lock:
            req = max(self.requests, 1)
            cache_total = max(self.cache_hits + self.cache_misses, 1)
            return {
                "uptime_seconds": round(time.time() - self.start_time, 1),
                "total_requests": self.requests,
                "error_rate": round(self.errors / req, 4),
                "avg_latency_ms": round(self.total_latency_ms / req, 2),
                "cache_hit_rate": round(self.cache_hits / cache_total, 4),
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
            }


METRICS = Metrics()
