"""Local HTTP API between Eggdrop (Tcl) and the backend.

Deliberately simple protocol (plain UTF-8 text, no JSON needed in Tcl):
  POST /api/v1/command   (form: network, channel, nick, handle, admin, command, args)
       → "OK\n" + lines "C <text>" (reply in channel/private) or "N <text>" (NOTICE to the user)
  GET  /api/v1/outbox?network=X&limit=N
       → lines "<id>\t<PRIVMSG|NOTICE>\t<target>\t<text>"
  POST /api/v1/heartbeat (form: network, botnick, server, channels)
Every /api route requires the X-Bot-Token header. /health is public (status only).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..app import App
from ..irc.commands import Ctx
from ..utils.text import irc_safe
from .charts import render_svg

log = logging.getLogger("cs2.api")
TEXT = "text/plain; charset=utf-8"


def build_api(app: App) -> FastAPI:
    api = FastAPI(title="CS2 Intelligence Backend", docs_url=None, redoc_url=None, openapi_url=None)
    token = str(app.cfg.get("settings.api.token", "") or "")
    allowed = [ipaddress.ip_network(n.strip()) for n in str(app.cfg.get("settings.api.allowed_ips", "127.0.0.1/32,::1/128")).split(",") if n.strip()]

    async def auth(request: Request) -> None:
        client = request.client.host if request.client else "0.0.0.0"
        try:
            ip = ipaddress.ip_address(client)
            if allowed and not any(ip in n for n in allowed):
                raise HTTPException(403, "IP not allowed")
        except ValueError:
            if client not in ("testclient",):
                raise HTTPException(403, "invalid IP")
        sent = request.headers.get("X-Bot-Token", "")
        if not token or not hmac.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(401, "invalid token")

    @api.get("/health")
    async def health():
        rep = await app.health_report()
        brief = {k: rep[k] for k in ("status", "db", "uptime_s", "paused", "failing_tasks", "live_matches")}
        brief["sources"] = {s["name"]: s["status"] for s in rep["sources"]}
        return JSONResponse(brief, status_code=200 if rep["status"] == "ok" else 503)

    @api.get("/api/v1/status", dependencies=[Depends(auth)])
    async def status():
        return JSONResponse(await app.health_report())

    @api.post("/api/v1/command", dependencies=[Depends(auth)])
    async def command(network: str = Form(...), nick: str = Form(...), command: str = Form(...),
                      channel: str = Form(""), handle: str = Form("*"), admin: str = Form("0"), args: str = Form("")):
        ctx = Ctx(network=irc_safe(network)[:50], channel=irc_safe(channel)[:100], nick=irc_safe(nick)[:50],
                  handle=irc_safe(handle)[:50] or "*", is_admin=admin == "1", command=command, args=args)
        reply = await app.commands.handle(ctx)
        prefix = "N" if reply.notice else "C"
        body = "OK\n" + "\n".join(f"{prefix} {irc_safe(ln)}" for ln in reply.lines if ln)
        return PlainTextResponse(body, media_type=TEXT)

    @api.get("/api/v1/outbox", dependencies=[Depends(auth)])
    async def outbox(network: str, limit: int = 8):
        rows = await app.publisher.fetch(irc_safe(network)[:50], max(1, min(limit, 30)))
        body = "\n".join(f"{r.id}\t{r.kind}\t{r.target}\t{irc_safe(r.text)}" for r in rows)
        return PlainTextResponse(body, media_type=TEXT)

    @api.post("/api/v1/heartbeat", dependencies=[Depends(auth)])
    async def heartbeat(network: str = Form(...), botnick: str = Form(""), server: str = Form(""),
                        channels: str = Form("")):
        app.heartbeats[network] = {"botnick": botnick, "server": server, "channels": channels.split(), "ts": time.time()}
        return PlainTextResponse("OK", media_type=TEXT)

    @api.get("/charts/{item_id}.svg")
    async def chart(item_id: int, days: int = 90):
        if not app.cfg.get("settings.api.public_charts", False):
            raise HTTPException(404)
        ref = app.market.by_id.get(item_id)
        if not ref:
            raise HTTPException(404)
        series = await app.market.series(item_id, max(7, min(days, 365)))
        return Response(render_svg(ref.key, series, app.market.base_currency), media_type="image/svg+xml")

    return api
