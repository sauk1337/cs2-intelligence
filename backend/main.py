"""Entry point: `python -m backend.main` (or `cs2intel` in Docker)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import uvicorn

from .app import App, _abs
from .core.config import ROOT, Config
from .core.logging_setup import setup_logging

log = logging.getLogger("cs2.main")


async def serve(cfg: Config) -> None:
    from .api.server import build_api

    app = App(cfg)
    await app.start()
    api = build_api(app)
    host = str(cfg.get("settings.api.host", "127.0.0.1"))
    port = int(cfg.get("settings.api.port", 8787))
    server = uvicorn.Server(uvicorn.Config(api, host=host, port=port, log_level="warning", access_log=False,
                                           proxy_headers=False, timeout_keep_alive=30))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    server_task = asyncio.create_task(server.serve())
    log.info("API listening on http://%s:%d", host, port)
    done, _ = await asyncio.wait({server_task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    await asyncio.gather(server_task, return_exceptions=True)
    await app.stop()
    log.info("Backend stopped")


async def init_db(cfg: Config) -> None:
    app = App(cfg)
    await app.db.init()
    await app.db.close()
    print("Database initialised")


async def run_once(cfg: Config, what: str) -> int:
    """One-off tasks for cron/scripts: backup, maintenance, check (real check of every source)."""
    from .database.maintenance import backup, cleanup, rollup_snapshots

    app = App(cfg)
    await app.start(run_scheduler=False)
    rc = 0
    try:
        if what == "backup":
            p = await backup(app.db, app.backup_dir, int(cfg.get("settings.maintenance.backup_keep", 14)))
            print(f"Backup: {p}")
        elif what == "maintenance":
            await rollup_snapshots(app.db, int(cfg.get("settings.maintenance.raw_snapshot_days", 7)))
            print(await cleanup(app.db, cfg.get("settings.maintenance.retention", {}) or {}))
        elif what == "check":
            rc = await check_sources(app)
    finally:
        await app.stop()
    return rc


async def check_sources(app: App) -> int:
    """Makes ONE real request to every enabled source and reports whether it works."""
    from .core.http import SourceError

    results = []

    async def probe(name, coro):
        t0 = asyncio.get_running_loop().time()
        try:
            n = await coro
            results.append((name, "OK", f"{n} items", asyncio.get_running_loop().time() - t0))
        except SourceError as e:
            results.append((name, "FAIL", str(e)[:120], 0))
        except Exception as e:  # broken parser, changed format...
            results.append((name, "ERROR", f"{type(e).__name__}: {e}"[:120], 0))

    async def count(c):
        r = await c
        return len(r) if hasattr(r, "__len__") else r

    await probe("ecb", count(_fx(app)))
    for name in app.news.fetcher.feeds():
        if app.cfg.source_enabled(name):
            await probe(name, count(app.news.fetcher.fetch(name)))
    m = app.market.adapters
    if m["skinport"].enabled:
        await probe("skinport", count(m["skinport"].fetch_bulk()))
    if m["steam"].enabled:
        await probe("steam", count(_one(m["steam"].fetch_item("AK-47 | Redline (Field-Tested)"))))
    if m["csfloat"].enabled:
        await probe("csfloat", count(_one(m["csfloat"].fetch_item("AK-47 | Redline (Field-Tested)"))))
    for name in ("buff163", "pricempire"):
        if m[name].enabled:
            await probe(name, count(m[name].fetch_bulk()))
    es = app.esports
    if es.source_ready("liquipedia"):
        await probe("liquipedia", count(es.liquipedia.matches()))
    if es.source_ready("hltv"):
        await probe("hltv", count(es.hltv.matches()))
    if es.source_ready("pandascore"):
        await probe("pandascore", count(es.pandascore.matches()))
    if es.source_ready("faceit"):
        await probe("faceit", count(es.faceit.matches()))
    if es.source_ready("vrs"):
        await probe("vrs", count(_second(es.vrs.latest())))
    for name in ("csfloat", "buff163", "pricempire", "hltv", "pandascore", "faceit"):
        c = app.cfg.source(name)
        if not c.get("enabled"):
            results.append((name, "OFF", "disabled in sources.yaml", 0))
    width = max(len(r[0]) for r in results)
    for name, st, msg, t in results:
        print(f"{name:<{width}}  {st:<6} {msg} {f'({t:.1f}s)' if t else ''}")
    return 0 if all(r[1] in ("OK", "OFF") for r in results) else 1


async def _fx(app: App):
    await app.market.refresh_fx()
    return app.market.fx.rates


async def _one(c):
    r = await c
    return [r] if r else []


async def _second(c):
    return (await c)[1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cs2intel")
    p.add_argument("command", nargs="?", default="serve",
                   choices=["serve", "init-db", "check", "backup", "maintenance"])
    p.add_argument("--config-dir", default=None)
    a = p.parse_args(argv)
    cfg = Config.load(a.config_dir)
    setup_logging(_abs(cfg.get("settings.paths.logs", "logs")), cfg.get("settings.logging.level", "INFO"),
                  int(cfg.get("settings.logging.max_bytes", 10_000_000)), int(cfg.get("settings.logging.backups", 7)))
    if a.command == "init-db":
        asyncio.run(init_db(cfg))
        return 0
    if a.command in ("check", "backup", "maintenance"):
        return asyncio.run(run_once(cfg, a.command))
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
