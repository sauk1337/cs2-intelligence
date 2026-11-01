"""Health registry of every source (for failover, !admin sources and /health)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class SourceHealth:
    name: str
    status: str = "unknown"          # ok | degraded | down | blocked | disabled | unknown
    last_ok: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    consecutive_errors: int = 0
    requests: int = 0
    errors: int = 0
    blocked_until: float = 0.0
    latency_ms: float | None = None
    notes: str = ""
    incidents: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status, "last_ok": self.last_ok,
            "last_error": self.last_error, "last_error_at": self.last_error_at,
            "consecutive_errors": self.consecutive_errors, "requests": self.requests,
            "errors": self.errors, "blocked_until": self.blocked_until or None,
            "latency_ms": self.latency_ms, "notes": self.notes,
        }


class HealthRegistry:
    def __init__(self) -> None:
        self._s: dict[str, SourceHealth] = {}
        self._listeners: list[Callable[[str, str, str], None]] = []

    def on_incident(self, cb: Callable[[str, str, str], None]) -> None:
        """cb(source, old_status, new_status) — used to record incidents in the database."""
        self._listeners.append(cb)

    def get(self, name: str) -> SourceHealth:
        if name not in self._s:
            self._s[name] = SourceHealth(name)
        return self._s[name]

    def _set(self, h: SourceHealth, status: str, detail: str = "") -> None:
        if h.status != status:
            old = h.status
            h.status = status
            h.incidents.append((time.time(), old, status, detail))
            del h.incidents[:-20]
            for cb in self._listeners:
                try:
                    cb(h.name, old, status)
                except Exception:  # pragma: no cover - a listener must never break this
                    pass

    def ok(self, name: str, latency_ms: float | None = None) -> None:
        h = self.get(name)
        h.requests += 1
        h.last_ok = time.time()
        h.consecutive_errors = 0
        h.latency_ms = latency_ms
        self._set(h, "ok")

    def error(self, name: str, message: str) -> None:
        h = self.get(name)
        h.requests += 1
        h.errors += 1
        h.consecutive_errors += 1
        h.last_error = message[:300]
        h.last_error_at = time.time()
        self._set(h, "down" if h.consecutive_errors >= 3 else "degraded", message[:120])

    def blocked(self, name: str, message: str, until: float) -> None:
        h = self.get(name)
        h.requests += 1
        h.errors += 1
        h.consecutive_errors += 1
        h.last_error = message[:300]
        h.last_error_at = time.time()
        h.blocked_until = until
        self._set(h, "blocked", message[:120])

    def disabled(self, name: str, notes: str = "") -> None:
        h = self.get(name)
        h.notes = notes
        self._set(h, "disabled")

    def is_available(self, name: str) -> bool:
        h = self.get(name)
        if h.status == "disabled":
            return False
        return time.time() >= h.blocked_until

    def all(self) -> list[SourceHealth]:
        return sorted(self._s.values(), key=lambda h: h.name)
