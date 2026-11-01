"""English output, mIRC colour safety and !alerts groups (SYNTHETIC data)."""
import re
import time

from backend.irc.commands import Ctx
from backend.irc.format import Formatter
from backend.sources.esports.base import LiveMatch, MapState
from backend.sources.market.base import Quote, SalesStats
from backend.utils.text import color, split_irc, strip_codes

# A colour code opens with \x03 + 1-2 digits [+ ,bg]. A bare \x03 (close) must never be followed by a digit
BAD_CLOSE = re.compile(r"\x03(?!\d)(?=[\d,])")


def assert_safe(line: str) -> None:
    for m in re.finditer(r"\x03", line):
        nxt = line[m.end():m.end() + 1]
        prev_open = re.match(r"\d{1,2}(,\d{1,2})?", line[m.end():])
        if not prev_open:  # closing code
            assert not nxt.isdigit() and nxt != ",", f"unsafe colour close in {line!r}"
    for chunk in split_irc(line):
        assert len(chunk.encode()) <= 400


def test_colour_close_never_followed_by_digit():
    s = color("x", "red") + "5 sales"
    assert strip_codes(s) == "x5 sales"
    assert_safe(s)


async def _seed(app):
    name = "AK-47 | Redline (Field-Tested)"
    await app.market.ingest_quotes([Quote(name, "skinport", "EUR", price=17.0, listings=40, url="https://x/sp")], snapshot=True)
    await app.market.ingest_quotes([Quote(name, "csfloat", "USD", price=22.0, url="https://x/cf")], snapshot=True)
    w = {"min": 18, "max": 24, "avg": 20, "median": 20, "volume": 0}
    await app.market.ingest_stats([SalesStats(name, "skinport", "EUR", windows={
        "24h": {**w, "volume": 30}, "7d": {**w, "volume": 210}, "30d": {**w, "volume": 900},
        "90d": {**w, "median": 19.5, "volume": 2700}})])


async def test_colourful_english_output_is_safe(app):
    app.fmt.colors = True
    await _seed(app)
    lines = []
    for cmd, args in [("skin", "ak redline ft"), ("skinscore", "ak redline ft"), ("skincompare", "ak redline ft"),
                      ("skinprice", "ak redline ft"), ("market", "drops"), ("marketstats", ""), ("status", ""),
                      ("help", ""), ("alerts", "")]:
        r = await app.commands.handle(Ctx("testnet", "#test", "c", "*", True, cmd, args))
        lines += r.lines
    ev = (await app.market.analyze())[0]
    lines += app.fmt.opportunity(ev)
    m = LiveMatch("hltv", "1", "Vitality", "Spirit", "IEM Beijing 2026", best_of=3, status="live", score1=1,
                  maps=[MapState("Mirage", 13, 9, "done"), MapState("Nuke", 8, 6, "live")], current_map="Nuke")
    app.esports.obs["hltv"] = {"1": m}
    await app.esports.reconcile()
    lines.append(app.fmt.live_line(app.esports.live()[0]))
    assert any("\x03" in ln for ln in lines) and any("\x02" in ln for ln in lines)
    for ln in lines:
        assert_safe(ln)
    plain = " ".join(strip_codes(ln) for ln in lines)
    for word in ("Price", "Change:", "Liquidity", "Score", "CS2 LIVE", "Vitality 1-0 Spirit", "Map Nuke 8-6"):
        assert word in plain, word
    for spanish in ("Precio", "Variación", "Liquidez", "Fuente", "ventas", "mediana", "hace "):
        assert spanish not in plain, spanish


def test_no_colours_mode_is_plain():
    f = Formatter(colors=False)
    assert f.tag("live") == "[CS2 LIVE]" and "\x03" not in f.label("EXCELLENT")


async def test_alert_groups_on_off(app):
    admin = Ctx("testnet", "#test", "boss", "owner", True, "alerts", "market off")
    r = await app.commands.handle(admin)
    assert "OFF" in strip_codes(r.lines[0]) and "market" in r.lines[0]
    for c in ("market", "opportunity", "arbitrage", "trend", "volume"):
        assert not await app.publisher.category_enabled("testnet", "#test", c)
    assert await app.publisher.category_enabled("testnet", "#test", "news")
    d = await app.publisher.publish("opportunity", ["x"])
    assert d[0].reason == "category_off"
    await app.commands.handle(Ctx("testnet", "#test", "boss", "owner", True, "alerts", "on all"))
    assert await app.publisher.category_enabled("testnet", "#test", "trend")
    r = await app.commands.handle(Ctx("testnet", "#test", "pleb", "*", False, "alerts", "off news"))
    assert "admin" in r.lines[0]
    r = await app.commands.handle(Ctx("testnet", "#test", "pleb", "*", False, "alerts", ""))
    assert "MARKET" in strip_codes(" ".join(r.lines)) and "LIVE" in strip_codes(" ".join(r.lines))
    # private: a user can mute their own watchlist alerts
    r = await app.commands.handle(Ctx("testnet", "", "pleb", "*", False, "alerts", "off watch"))
    assert (await app.publisher.prefs("testnet", "user", "nick:pleb")).get("watch") is False
