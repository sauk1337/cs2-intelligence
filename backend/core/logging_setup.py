"""Logging with file rotation + a buffer of recent errors for `!admin logs`."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import time
from pathlib import Path

RECENT: collections.deque = collections.deque(maxlen=300)


class _RecentHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECENT.append((record.created, record.levelname, record.name, record.getMessage()[:400]))
        except Exception:  # pragma: no cover
            pass


def setup_logging(log_dir: str | Path, level: str = "INFO", max_bytes: int = 10_000_000, backups: int = 7) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = logging.handlers.RotatingFileHandler(log_dir / "backend.log", maxBytes=max_bytes, backupCount=backups,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    eh = logging.handlers.RotatingFileHandler(log_dir / "errors.log", maxBytes=max_bytes, backupCount=backups,
                                              encoding="utf-8")
    eh.setLevel(logging.WARNING)
    eh.setFormatter(fmt)
    root.addHandler(eh)
    rh = _RecentHandler()
    rh.setLevel(logging.WARNING)
    root.addHandler(rh)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def recent(limit: int = 10, min_level: str = "WARNING") -> list[tuple]:
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    lvl = order.get(min_level, 30)
    items = [r for r in RECENT if order.get(r[1], 0) >= lvl]
    return items[-limit:]


def fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
