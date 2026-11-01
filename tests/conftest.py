"""Test fixtures.

IMPORTANT: tests ALWAYS use their own temporary database and never touch data/.
- tests/fixtures/*  → real responses captured from the sources (only used to test parsers).
- Market/match data built inside the tests is SYNTHETIC and labelled as such.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

os.environ.setdefault("BOT_API_TOKEN", "test-token")
os.environ.setdefault("IRC_NETWORK", "testnet")
os.environ.setdefault("IRC_MAIN_CHANNEL", "#test")

from backend.app import App  # noqa: E402
from backend.core.config import Config  # noqa: E402


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def cfg() -> Config:
    c = Config.load(ROOT / "config", env_file=ROOT / "nonexistent.env")
    # Disable external sources in tests unless a test enables them explicitly
    for sec in ("news", "market", "esports"):
        for name, v in (c.sources.get(sec) or {}).items():
            if isinstance(v, dict):
                v["enabled"] = False
    c._data["settings"]["networks"] = {"testnet": {"channels": {"#test": {
        "categories": ["news", "valve", "market", "opportunity", "arbitrage", "trend", "volume", "live", "results",
                       "upcoming"],
        "min_news_relevance": 50, "min_opportunity_score": 60, "min_live_importance": 30,
        "max_per_hour": 50, "quiet_hours": "", "silent": False}}}}
    c._data["settings"]["irc"]["colors"] = False
    return c


@pytest_asyncio.fixture
async def app(cfg, tmp_path):
    a = App(cfg, db_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
            http_transport=httpx.MockTransport(lambda req: httpx.Response(599, text="no network in tests")))
    a.backup_dir = tmp_path / "backups"
    await a.start(run_scheduler=False)
    a.market.fx.rates = {"EUR": 1.0, "USD": 1.10, "CNY": 7.80}  # SYNTHETIC rates for tests
    yield a
    await a.stop()
