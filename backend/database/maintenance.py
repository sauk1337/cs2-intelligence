"""Maintenance: history compaction, cleanup of old data and backups."""
from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from sqlalchemy import and_, delete, func, select

from .db import Database
from .models import AlertLog, CommandLog, News, Opportunity, Outbox, PriceSnapshot, SystemLog

log = logging.getLogger("cs2.maintenance")
DAY = 86400


async def rollup_snapshots(db: Database, raw_days: int = 7) -> int:
    """Turn `raw` snapshots older than `raw_days` into one daily row per item/source.

    The daily row keeps: closing price (last), average median, last bid and maximum volume.
    Shrinks the database ~24x without losing the daily series needed for 30/90/365 days.
    """
    cutoff = time.time() - raw_days * DAY
    cutoff = cutoff - (cutoff % DAY)  # align to UTC midnight
    created = 0
    async with db.session() as s:
        day_expr = func.floor(PriceSnapshot.ts / DAY)
        groups = (await s.execute(
            select(PriceSnapshot.item_id, PriceSnapshot.source, day_expr.label("d"))
            .where(and_(PriceSnapshot.granularity == "raw", PriceSnapshot.ts < cutoff))
            .group_by(PriceSnapshot.item_id, PriceSnapshot.source, day_expr)
            .limit(50000)
        )).all()
        for item_id, source, d in groups:
            rows = (await s.execute(
                select(PriceSnapshot).where(and_(PriceSnapshot.item_id == item_id, PriceSnapshot.source == source,
                                                 PriceSnapshot.granularity == "raw",
                                                 PriceSnapshot.ts >= d * DAY, PriceSnapshot.ts < (d + 1) * DAY))
                .order_by(PriceSnapshot.ts)
            )).scalars().all()
            if not rows:
                continue
            last = rows[-1]
            prices = [r.price for r in rows if r.price is not None]
            medians = [r.median for r in rows if r.median is not None]
            vols = [r.volume_24h for r in rows if r.volume_24h is not None]
            lst = [r.listings for r in rows if r.listings is not None]
            s.add(PriceSnapshot(
                item_id=item_id, source=source, ts=d * DAY + DAY / 2, currency=last.currency,
                price=last.price if last.price is not None else (prices[-1] if prices else None),
                median=sum(medians) / len(medians) if medians else None,
                bid=last.bid, volume_24h=max(vols) if vols else None,
                listings=int(sum(lst) / len(lst)) if lst else None,
                raw_currency=last.raw_currency, raw_price=last.raw_price, granularity="daily"))
            for r in rows:
                await s.delete(r)
            created += 1
    if created:
        log.info("Compacted %d daily price series", created)
    return created


async def cleanup(db: Database, retention: dict) -> dict:
    t = time.time()
    res = {}
    async with db.session() as s:
        specs = [
            ("price_daily", delete(PriceSnapshot).where(PriceSnapshot.ts < t - retention.get("prices_days", 400) * DAY)),
            ("news", delete(News).where(News.published_at < t - retention.get("news_days", 365) * DAY)),
            ("opportunities", delete(Opportunity).where(Opportunity.ts < t - retention.get("opportunities_days", 120) * DAY)),
            ("outbox", delete(Outbox).where(Outbox.created_at < t - retention.get("outbox_days", 3) * DAY)),
            ("alert_log", delete(AlertLog).where(AlertLog.ts < t - retention.get("alerts_days", 60) * DAY)),
            ("command_log", delete(CommandLog).where(CommandLog.ts < t - retention.get("commands_days", 90) * DAY)),
            ("system_log", delete(SystemLog).where(SystemLog.ts < t - retention.get("logs_days", 30) * DAY)),
        ]
        for name, stmt in specs:
            r = await s.execute(stmt)
            res[name] = r.rowcount or 0
    log.info("Cleanup: %s", res)
    return res


def _sqlite_backup(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(str(src))
    tmp = dest.with_suffix(".tmp")
    dst_conn = sqlite3.connect(str(tmp))
    with dst_conn:
        src_conn.backup(dst_conn)  # consistent copy even while writes happen (online backup API)
    dst_conn.close()
    src_conn.close()
    with open(tmp, "rb") as fi, gzip.open(dest, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    tmp.unlink(missing_ok=True)


async def backup(db: Database, backup_dir: str | Path, keep: int = 14) -> Path | None:
    if not db.is_sqlite:
        log.info("Backup: with PostgreSQL use pg_dump (scripts/backup.sh)")
        return None
    src = Path(db.url.split("///", 1)[1])
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"cs2intel-{time.strftime('%Y%m%d-%H%M%S')}.db.gz"
    await asyncio.to_thread(_sqlite_backup, src, dest)
    olds = sorted(backup_dir.glob("cs2intel-*.db.gz"))
    for old in olds[:-keep]:
        old.unlink(missing_ok=True)
    log.info("Backup created: %s", dest.name)
    return dest
