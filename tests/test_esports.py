"""Multi-source LIVE reconciliation and event detection (SYNTHETIC matches)."""
import time

from backend.sources.esports.base import LiveMatch, MapState


def lm(src, sid, status="live", s1=0, s2=0, maps=None, cur=None, start=None, inferred=False, t1="Vitality",
       t2="Spirit", event="IEM Beijing 2026", age=0):
    m = LiveMatch(source=src, source_id=sid, team1=t1, team2=t2, event=event, best_of=3, status=status,
                  start_time=start, score1=s1, score2=s2, maps=maps or [], current_map=cur, tier=1, inferred=inferred)
    m.fetched_at = time.time() - age
    return m


def feed(app, src, *ms):
    app.esports.obs.setdefault(src, {})
    for m in ms:
        app.esports.obs[src][m.source_id] = m


async def test_merge_two_sources_prefers_priority(app):
    feed(app, "liquipedia", lm("liquipedia", "a", s1=0, s2=0, start=time.time() - 600))
    feed(app, "hltv", lm("hltv", "1", s1=1, s2=0, maps=[MapState("Mirage", 13, 9, "done"), MapState("Inferno", 4, 2, "live")],
                         cur="Inferno"))
    await app.esports.reconcile()
    live = app.esports.live()
    assert len(live) == 1
    m = live[0]
    assert m["source"] == "hltv" and set(m["source_ids"]) == {"hltv", "liquipedia"}
    assert m["sync_status"] == "conflict"  # liquipedia says 0-0 and hltv 1-0, both fresh
    assert m["confidence"] < 0.95


async def test_reversed_team_order_is_same_match(app):
    feed(app, "pandascore", lm("pandascore", "p", s1=1, s2=0))
    feed(app, "faceit", lm("faceit", "f", s1=0, s2=1, t1="Spirit", t2="Vitality"))
    await app.esports.reconcile()
    m = app.esports.live()[0]
    assert len(app.esports.live()) == 1 and m["sync_status"] == "ok"


async def test_events_start_map_end_finish(app):
    events = []

    async def capture(evs):
        events.extend(evs)
        await app.alerts.on_match_events(evs)  # capture and also publish, as in production
    app.esports.on_events = capture
    feed(app, "hltv", lm("hltv", "1", status="upcoming", start=time.time() + 60))
    await app.esports.reconcile()
    feed(app, "hltv", lm("hltv", "1", maps=[MapState("Mirage", 3, 1, "live")], cur="Mirage"))
    await app.esports.reconcile()
    feed(app, "hltv", lm("hltv", "1", s1=1, maps=[MapState("Mirage", 13, 9, "done"), MapState("Nuke", 0, 0, "live")], cur="Nuke"))
    await app.esports.reconcile()
    feed(app, "hltv", lm("hltv", "1", status="finished", s1=2, s2=0,
                         maps=[MapState("Mirage", 13, 9, "done"), MapState("Nuke", 13, 5, "done")]))
    await app.esports.reconcile()
    kinds = [e.kind for e in events]
    assert kinds[0] == "match_start" and "map_end" in kinds and kinds[-1] == "match_end"
    assert "map_change" in kinds
    fin = app.esports.finished()[0]
    assert (fin["score1"], fin["score2"]) == (2, 0)
    # the result is posted to the channel (results category)
    out = [r.text for r in await app.publisher.fetch("testnet", 30)]
    assert any("[RESULT]" in t and "Vitality 2-0 Spirit" in t and "Vitality win" in t for t in out)


async def test_inferred_and_stale_never_announced(app):
    events = []

    async def capture(evs):
        events.extend(evs)
    app.esports.on_events = capture
    feed(app, "liquipedia", lm("liquipedia", "x", inferred=True, start=time.time() - 60))
    await app.esports.reconcile()
    assert events == [] and app.esports.live()[0]["sync_status"] == "inferred"
    feed(app, "hltv", lm("hltv", "y", t1="G2", t2="MOUZ", age=3600))
    await app.esports.reconcile()
    g2 = app.esports.find("g2")
    assert g2["sync_status"] == "stale" and not any(e.match["key"] == g2["key"] for e in events)


async def test_follow_and_commands(app):
    from backend.irc.commands import Ctx
    r = await app.commands.handle(Ctx("testnet", "#test", "bob", "*", False, "followmatch", "Vitality"))
    assert "Following Vitality" in r.lines[0]
    feed(app, "hltv", lm("hltv", "1", s1=1, maps=[MapState("Mirage", 13, 9, "done")], cur="Nuke"))
    await app.esports.reconcile()
    r = await app.commands.handle(Ctx("testnet", "#test", "bob", "*", False, "live", ""))
    assert "Vitality 1-0" in r.lines[0] and "Map Nuke" in r.lines[0]
    r = await app.commands.handle(Ctx("testnet", "#test", "bob", "*", False, "match", "spirit"))
    assert any("Maps: Mirage 13:9" in ln for ln in r.lines)
    r = await app.commands.handle(Ctx("testnet", "#test", "bob", "*", False, "livealerts", ""))
    assert "Vitality" in r.lines[0]
    r = await app.commands.handle(Ctx("testnet", "#test", "bob", "*", False, "matchstats", "vitality"))
    assert "does not provide" in r.lines[0]
