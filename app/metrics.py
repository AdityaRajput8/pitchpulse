"""Process-wide counters shown on the Observability tab."""
from __future__ import annotations

import time
from collections import deque


class Metrics:
    def __init__(self):
        self.started = time.time()
        self.c = {k: 0 for k in (
            "balls_processed", "alerts_sent", "balls_filtered", "llm_calls", "llm_ok",
            "template_fallbacks", "guard_rejections", "cache_hits", "sessions_created",
        )}
        self.latency = deque(maxlen=200)
        self.provider_hits: dict[str, int] = {}
        self.bus_backend = "memory"
        self.last_llm_error = None
        self.llm_providers: list[str] = []

    def inc(self, key: str, n: int = 1):
        self.c[key] = self.c.get(key, 0) + n

    def snapshot(self, active_sessions: int) -> dict:
        lat = list(self.latency)
        llm = max(self.c["llm_calls"], 1)
        return {
            **self.c,
            "active_sessions": active_sessions,
            "avg_llm_latency_ms": round(sum(lat) / len(lat)) if lat else 0,
            "p95_llm_latency_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1]) if len(lat) >= 5 else None,
            "guard_reject_rate": round(self.c["guard_rejections"] / llm, 3),
            "noise_filtered_pct": round(
                100 * self.c["balls_filtered"] / max(self.c["balls_processed"], 1), 1),
            "providers": self.provider_hits,
            "event_bus": self.bus_backend,
            "llm_providers": self.llm_providers,
            "last_llm_error": self.last_llm_error,
            "uptime_s": round(time.time() - self.started),
        }


metrics = Metrics()
