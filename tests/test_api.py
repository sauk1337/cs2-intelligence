"""Eggdrop⇄backend HTTP API: authentication, text protocol, outbox, malicious commands."""
from fastapi.testclient import TestClient

from backend.api.server import build_api

TOKEN = {"X-Bot-Token": "test-token"}


def form(**kw):
    base = {"network": "testnet", "channel": "#test", "nick": "eve", "handle": "*", "admin": "0", "args": ""}
    base.update(kw)
    return base


async def test_auth_and_protocol(app):
    c = TestClient(build_api(app))
    assert c.post("/api/v1/command", data=form(command="version")).status_code == 401
    assert c.post("/api/v1/command", data=form(command="version"), headers={"X-Bot-Token": "wrong"}).status_code == 401
    r = c.post("/api/v1/command", data=form(command="version"), headers=TOKEN)
    assert r.status_code == 200 and r.text.startswith("OK\nC CS2 Intelligence Bot")
    assert c.get("/health").json()["db"] is True


async def test_injection_is_neutralised(app):
    c = TestClient(build_api(app))
    r = c.post("/api/v1/command", data=form(command="skin", args="ak\r\nQUIT :bye\x01"), headers=TOKEN)
    assert "\r" not in r.text and r.text.count("\n") >= 1
    for line in r.text.split("\n")[1:]:
        assert not line.startswith("QUIT") and line[:2] in ("C ", "N ")


async def test_admin_requires_flag(app):
    c = TestClient(build_api(app))
    assert "Permission denied" in c.post("/api/v1/command", data=form(command="admin", args="pause"), headers=TOKEN).text
    r = c.post("/api/v1/command", data=form(command="admin", args="pause", admin="1", handle="owner"), headers=TOKEN)
    assert "PAUSED" in r.text and app.publisher.paused
    c.post("/api/v1/command", data=form(command="admin", args="resume", admin="1"), headers=TOKEN)
    assert not app.publisher.paused


async def test_outbox_format(app):
    await app.publisher.enqueue("testnet", "#test", ["line one", "line\ttwo"])
    c = TestClient(build_api(app))
    r = c.get("/api/v1/outbox", params={"network": "testnet"}, headers=TOKEN)
    lines = r.text.split("\n")
    assert len(lines) == 2 and lines[0].split("\t")[1:] == ["PRIVMSG", "#test", "line one"]
    assert c.get("/api/v1/outbox", params={"network": "testnet"}, headers=TOKEN).text == ""


async def test_pagination_and_rate_limit(app):
    from backend.irc.commands import Ctx, Reply
    router = app.commands

    async def many(ctx):
        return Reply([f"l{i}" for i in range(12)])
    router.handlers["help"] = many
    r = await router.handle(Ctx("testnet", "#test", "pag", command="help"))
    assert r.lines[-1].startswith("… 7 more") and len(r.lines) == 6
    r2 = await router.handle(Ctx("testnet", "#test", "pag", command="more"))
    assert r2.lines[0] == "l5"
    for _ in range(10):
        r = await router.handle(Ctx("testnet", "#test", "flooder", command="version"))
    assert "Slow down" in r.lines[0]
