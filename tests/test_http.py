"""HTTP client: retries, rate limits, blocks (no evasion), cache and conditional GET."""
import httpx
import pytest

from backend.core.cache import TTLCache
from backend.core.health import HealthRegistry
from backend.core.http import HttpClient, SourceBlocked, SourceError


def client(cfg, handler):
    cfg._data["sources"]["http_defaults"] = {"min_interval": 0, "retries": 2, "block_cooldown": 600}
    return HttpClient(cfg, HealthRegistry(), TTLCache(), transport=httpx.MockTransport(handler))


async def test_retry_on_5xx_then_ok(cfg, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, text="ok")
    c = client(cfg, h)
    r = await c.get("src", "https://x.test/a")
    assert r.text == "ok" and len(calls) == 3 and c.health.get("src").status == "ok"
    await c.close()


async def test_challenge_blocks_and_pauses(cfg):
    calls = []

    def h(req):
        if req.url.path != "/robots.txt":  # hltv honours robots.txt: that request doesn't count
            calls.append(1)
        return httpx.Response(403, text="<html><title>Just a moment...</title>challenge-platform</html>")
    c = client(cfg, h)
    with pytest.raises(SourceBlocked):
        await c.get("hltv", "https://x.test/matches")
    assert c.health.get("hltv").status == "blocked"
    with pytest.raises(SourceBlocked):  # paused: not even requested again
        await c.get("hltv", "https://x.test/matches")
    assert len(calls) == 1
    await c.close()


async def test_429_retry_after(cfg, monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    seq = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")]
    c = client(cfg, lambda req: seq.pop(0))
    assert (await c.get("s", "https://x.test")).text == "ok"
    assert 7 in waits
    await c.close()


async def test_cache_and_conditional_get(cfg):
    calls = []

    def h(req):
        calls.append(req.headers.get("If-None-Match"))
        if req.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="feed", headers={"ETag": '"v1"'})
    c = client(cfg, h)
    r1 = await c.get("rss", "https://x.test/feed", conditional=True)
    r2 = await c.get("rss", "https://x.test/feed", conditional=True)
    assert r1.text == r2.text == "feed" and r2.not_modified and calls == [None, '"v1"']
    await c.get("api", "https://x.test/j", cache_ttl=60)
    await c.get("api", "https://x.test/j", cache_ttl=60)
    assert len(calls) == 3  # the second, cached request never hits the network
    await c.close()


async def test_auth_header_and_cookie_from_config(cfg):
    seen = {}

    def h(req):
        seen.update(req.headers)
        return httpx.Response(200, text="ok")
    cfg._data["sources"]["esports"]["hltv"]["auth_header"] = "X-Partner-Key: abc"
    cfg._data["sources"]["esports"]["hltv"]["cookie"] = "sess=1"
    cfg._data["sources"]["esports"]["hltv"]["http"]["respect_robots"] = False
    c = client(cfg, h)
    await c.get("hltv", "https://x.test/")
    assert seen.get("x-partner-key") == "abc" and "sess=1" in seen.get("cookie", "")
    await c.close()


async def test_unexpected_status_raises(cfg):
    c = client(cfg, lambda req: httpx.Response(404))
    with pytest.raises(SourceError):
        await c.get("s", "https://x.test")
    await c.close()


async def _nosleep(*_a, **_k):
    return None
