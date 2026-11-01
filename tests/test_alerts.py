"""Posting, cooldown, limits, quiet hours, pause and watchlists (SYNTHETIC data)."""
import time

from backend.alerts.publisher import Publisher
from backend.database.models import IrcUser, Watch
from backend.irc.commands import Ctx
from backend.sources.market.base import Quote


async def test_publish_and_fetch_at_most_once(app):
    d = await app.publisher.publish("news", ["hola", "mundo"], dedupe_key="n1", cooldown_s=3600)
    assert d[0].ok
    rows = await app.publisher.fetch("testnet", 10)
    assert [r.text for r in rows] == ["hola", "mundo"] and rows[0].target == "#test"
    assert await app.publisher.fetch("testnet", 10) == []


async def test_cooldown_dedupe(app):
    await app.publisher.publish("news", ["a"], dedupe_key="k", cooldown_s=3600)
    d = await app.publisher.publish("news", ["a"], dedupe_key="k", cooldown_s=3600)
    assert not d[0].ok and d[0].reason == "cooldown"


async def test_hourly_limit_and_priority(app):
    app.cfg._data["settings"]["networks"]["testnet"]["channels"]["#test"]["max_per_hour"] = 2
    for i in range(2):
        assert (await app.publisher.publish("news", [f"x{i}"]))[0].ok
    assert (await app.publisher.publish("news", ["x3"]))[0].reason == "hourly_limit"
    assert (await app.publisher.publish("news", ["urgent"], priority=9))[0].ok


async def test_category_threshold_pause_silent(app):
    d = await app.publisher.publish("opportunity", ["x"], metric=10, metric_key="min_opportunity_score")
    assert d[0].reason == "below_threshold"
    await app.publisher.set_pref("testnet", "channel", "#test", "trend", False)
    assert (await app.publisher.publish("trend", ["x"]))[0].reason == "category_off"
    await app.publisher.set_paused(True)
    assert (await app.publisher.publish("news", ["x"]))[0].reason == "paused"
    await app.publisher.set_paused(False)
    app.cfg._data["settings"]["networks"]["testnet"]["channels"]["#test"]["silent"] = True
    assert (await app.publisher.publish("news", ["x"]))[0].reason == "silent"


def test_quiet_hours_windows():
    # 02:30 UTC (Europe/London is UTC+0 in November)
    ts = 1793500200  # about 2026-11-01 02:30 UTC
    assert Publisher.in_quiet_hours("23:00-07:00", "UTC", ts - (ts % 86400) + 2.5 * 3600)
    assert not Publisher.in_quiet_hours("23:00-07:00", "UTC", ts - (ts % 86400) + 12 * 3600)
    assert Publisher.in_quiet_hours("01:00-03:00", "UTC", ts - (ts % 86400) + 2 * 3600)
    assert not Publisher.in_quiet_hours("", "UTC")


async def test_long_lines_are_split(app):
    await app.publisher.enqueue("testnet", "#test", ["x " * 400])
    rows = await app.publisher.fetch("testnet", 10)
    assert len(rows) >= 2 and all(len(r.text.encode()) <= 400 for r in rows)


async def test_watchlist_threshold_and_target(app):
    await app.market.ingest_quotes([Quote("AK-47 | Redline (Field-Tested)", "skinport", "EUR", price=20.0)], snapshot=True)
    r = await app.commands.handle(Ctx("testnet", "#test", "alice", "*", False, "watch", "ak redline ft 5% <15"))
    assert "Watching" in r.lines[0]
    await app.alerts.check_watchlists()
    assert await app.publisher.fetch("testnet") == []  # no change yet
    await app.market.ingest_quotes([Quote("AK-47 | Redline (Field-Tested)", "skinport", "EUR", price=18.5)], snapshot=True)
    assert await app.alerts.check_watchlists() == 1
    msg = (await app.publisher.fetch("testnet"))[0]
    assert msg.target == "#test" and "alice" in msg.text and "-7.5%" in msg.text
    await app.market.ingest_quotes([Quote("AK-47 | Redline (Field-Tested)", "skinport", "EUR", price=14.9)], snapshot=True)
    await app.alerts.check_watchlists()
    assert "target price" in (await app.publisher.fetch("testnet"))[0].text
    r = await app.commands.handle(Ctx("testnet", "#test", "alice", "*", False, "watchlist", ""))
    assert "Redline" in " ".join(r.lines)
    r = await app.commands.handle(Ctx("testnet", "#test", "alice", "*", False, "unwatch", "ak redline ft"))
    assert "Stopped watching" in r.lines[0]
