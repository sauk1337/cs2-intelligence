"""Centralised configuration loading.

- `config/settings.yaml`  : general behaviour, IRC networks/channels, intervals, publishing.
- `config/sources.yaml`   : sources (enable/disable, URLs, limits, fees, authorisation).
- `config/scoring.yaml`   : weights of the opportunity engine.
- `.env`                  : secrets and deployment-specific values.

The YAML files support `${VAR}` and `${VAR:-default_value}` interpolation from the environment,
so credentials never need to be written into the YAML files.
"""
from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")
_SECRET_HINTS = ("token", "key", "secret", "password", "cookie", "auth")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        out = _ENV_RE.sub(repl, value)
        # Type conversion when the whole string was a single variable
        if _ENV_RE.fullmatch(value or ""):
            low = out.strip().lower()
            if low in ("true", "yes", "on"):
                return True
            if low in ("false", "no", "off"):
                return False
            if re.fullmatch(r"-?\d+", out.strip()):
                return int(out)
            if re.fullmatch(r"-?\d+\.\d+", out.strip()):
                return float(out)
        return out
    if isinstance(value, dict):
        # keys too (e.g. network or channel name defined in .env); always as text
        return {(str(_interpolate(k)) if isinstance(k, str) else k): _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Dotted-path access: cfg.get("market.min_price")."""

    def __init__(self, data: dict, config_dir: Path):
        self._data = data
        self.config_dir = config_dir
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, config_dir: str | Path | None = None, env_file: str | Path | None = None) -> "Config":
        config_dir = Path(config_dir or os.environ.get("CS2_CONFIG_DIR") or ROOT / "config")
        env_path = Path(env_file) if env_file else ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
        data: dict = {}
        for name in ("settings", "sources", "scoring"):
            path = config_dir / f"{name}.yaml"
            if path.exists():
                with path.open(encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                data[name] = _interpolate(raw)
            else:
                data[name] = {}
        local = config_dir / "local.yaml"  # optional, unversioned local overrides
        if local.exists():
            with local.open(encoding="utf-8") as fh:
                data = _deep_merge(data, _interpolate(yaml.safe_load(fh) or {}))
        return cls(data, config_dir)

    def reload(self) -> None:
        fresh = Config.load(self.config_dir)
        with self._lock:
            self._data = fresh._data

    # ---------------------------------------------------------------- reading
    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    @property
    def settings(self) -> dict:
        return self._data.get("settings", {})

    @property
    def sources(self) -> dict:
        return self._data.get("sources", {})

    @property
    def scoring(self) -> dict:
        return self._data.get("scoring", {})

    def source(self, name: str) -> dict:
        """Configuration of one source, searching every section of sources.yaml."""
        for section in ("news", "market", "esports", "reference"):
            sec = self.sources.get(section, {}) or {}
            if name in sec:
                return sec[name] or {}
        return {}

    def source_enabled(self, name: str) -> bool:
        return bool(self.source(name).get("enabled", False))

    def redacted(self) -> dict:
        """Copy without secrets for `!admin config` and /health."""
        def walk(obj: Any, key: str = "") -> Any:
            if isinstance(obj, dict):
                return {k: walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [walk(v, key) for v in obj]
            if isinstance(obj, str) and obj and any(h in key.lower() for h in _SECRET_HINTS):
                return "***"
            return obj
        return walk(self._data)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
