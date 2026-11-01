"""Database schema (SQLAlchemy 2.x). Portable SQLite ⇄ PostgreSQL.

Timestamps are stored as UTC epoch (float) so the schema is identical on
both engines and time-window calculations are trivial.
"""
from __future__ import annotations

from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 1


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ system
class KV(Base):
    """Runtime settings (pause flag, schema version, cursors...)."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class SourceRow(Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_ok: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=0)


class SystemLog(Base):
    __tablename__ = "system_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    level: Mapped[str] = mapped_column(String(10))
    component: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)


class CommandLog(Base):
    __tablename__ = "command_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    network: Mapped[str] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(100))
    nick: Mapped[str] = mapped_column(String(50))
    handle: Mapped[str] = mapped_column(String(50), default="*")
    command: Mapped[str] = mapped_column(String(50))
    args: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)


# ------------------------------------------------------------------- IRC
class IrcUser(Base):
    __tablename__ = "irc_users"
    __table_args__ = (UniqueConstraint("network", "ident"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(50))
    ident: Mapped[str] = mapped_column(String(100))   # Eggdrop handle or "nick:<nick>"
    last_nick: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)


class AlertPref(Base):
    """Alert categories enabled/disabled per channel or per user."""
    __tablename__ = "alert_prefs"
    __table_args__ = (UniqueConstraint("network", "scope", "target", "category"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(50))
    scope: Mapped[str] = mapped_column(String(10))    # channel | user
    target: Mapped[str] = mapped_column(String(100))  # #channel or user ident
    category: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Outbox(Base):
    """Messages waiting to be posted on IRC (Eggdrop collects them by polling)."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(50), index=True)
    target: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10), default="PRIVMSG")  # PRIVMSG | NOTICE
    category: Mapped[str] = mapped_column(String(30), default="")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    delivered_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


class AlertLog(Base):
    """History of emitted alerts (cooldowns, hourly limits, statistics)."""
    __tablename__ = "alert_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    network: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    suppressed: Mapped[str | None] = mapped_column(String(40), nullable=True)  # reason when it was not posted


# ---------------------------------------------------------------------- news
class News(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    title_norm: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    title_es: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_es: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str] = mapped_column(String(8), default="en")
    category: Mapped[str] = mapped_column(String(30), index=True, default="escena")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    relevance: Mapped[float] = mapped_column(Float, default=0)
    important: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[float] = mapped_column(Float, index=True)
    fetched_at: Mapped[float] = mapped_column(Float)
    posted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    processed_by: Mapped[str] = mapped_column(String(20), default="heuristic")  # heuristic | llm | translate
    body: Mapped[str] = mapped_column(Text, default="")  # full text (Valve patch notes)


# -------------------------------------------------------------------- market
class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(300), unique=True)
    base_name: Mapped[str] = mapped_column(String(300), index=True)   # without wear / StatTrak / Souvenir
    search_key: Mapped[str] = mapped_column(String(300), index=True)  # normalised for search
    item_type: Mapped[str] = mapped_column(String(30), index=True)    # weapon, knife, gloves, sticker, case...
    weapon: Mapped[str | None] = mapped_column(String(60), nullable=True)
    skin: Mapped[str | None] = mapped_column(String(120), nullable=True)
    exterior: Mapped[str | None] = mapped_column(String(30), nullable=True)
    stattrak: Mapped[bool] = mapped_column(Boolean, default=False)
    souvenir: Mapped[bool] = mapped_column(Boolean, default=False)
    phase: Mapped[str | None] = mapped_column(String(40), nullable=True)  # Doppler Phase 2, Ruby...
    collection: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rarity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tournament: Mapped[str | None] = mapped_column(String(80), nullable=True)  # Major stickers/capsules
    created_at: Mapped[float] = mapped_column(Float)


class PriceSnapshot(Base):
    """Price snapshot from one platform. `granularity` = raw | daily (after compaction)."""
    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snap_item_src_ts", "item_id", "source", "ts"), Index("ix_snap_ts", "ts"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(30))
    ts: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(5))           # base currency after conversion
    price: Mapped[float | None] = mapped_column(Float, nullable=True)        # lowest sell price (ask)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)       # median of sales/listings
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)          # best buy order
    volume_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)   # sales in 24 h
    listings: Mapped[int | None] = mapped_column(Integer, nullable=True)     # units for sale
    raw_currency: Mapped[str | None] = mapped_column(String(5), nullable=True)
    raw_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    granularity: Mapped[str] = mapped_column(String(8), default="raw")


class MarketLatest(Base):
    """Latest known data per item and source (fast lookups)."""
    __tablename__ = "market_latest"
    __table_args__ = (UniqueConstraint("item_id", "source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30))
    ts: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(5))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketStat(Base):
    """Aggregated sales statistics per window as published by the source (e.g. Skinport)."""
    __tablename__ = "market_stats"
    __table_args__ = (UniqueConstraint("item_id", "source", "window"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30))
    window: Mapped[str] = mapped_column(String(5))   # 24h | 7d | 30d | 90d
    ts: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(5))
    min: Mapped[float | None] = mapped_column(Float, nullable=True)
    max: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opp_ts_score", "ts", "score"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    ts: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(20))         # drop | arbitrage
    label: Mapped[str] = mapped_column(String(20))        # EXCELLENT | INTERESTING | WATCH | HIGH RISK
    score: Mapped[float] = mapped_column(Float)
    risk: Mapped[str] = mapped_column(String(10))         # LOW | MEDIUM | HIGH
    source: Mapped[str] = mapped_column(String(30))
    price: Mapped[float] = mapped_column(Float)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(5))
    details: Mapped[dict] = mapped_column(JSON, default=dict)  # components and explanation
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)


class Watch(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "item_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("irc_users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(100), default="")   # where to notify
    threshold_pct: Mapped[float] = mapped_column(Float, default=5.0)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_notified_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


# ------------------------------------------------------------------- esports
class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    norm: Mapped[str] = mapped_column(String(120), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(30), default="")
    updated_at: Mapped[float] = mapped_column(Float)


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nick: Mapped[str] = mapped_column(String(80), unique=True)
    norm: Mapped[str] = mapped_column(String(80), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(30), default="")
    updated_at: Mapped[float] = mapped_column(Float)


class Tournament(Base):
    __tablename__ = "tournaments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), index=True)   # upcoming | ongoing | concluded
    is_major: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[float] = mapped_column(Float)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (Index("ix_match_status_start", "status", "start_time"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)   # canonical identifier
    team1: Mapped[str] = mapped_column(String(120))
    team2: Mapped[str] = mapped_column(String(120))
    event: Mapped[str] = mapped_column(String(200), default="")
    stage: Mapped[str] = mapped_column(String(120), default="")
    best_of: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12))          # upcoming | live | finished
    start_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    score1: Mapped[int] = mapped_column(Integer, default=0)  # maps won
    score2: Mapped[int] = mapped_column(Integer, default=0)
    maps: Mapped[list] = mapped_column(JSON, default=list)   # [{name, s1, s2, status}]
    current_map: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(30))
    source_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # player stats when the source provides them
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    sync_status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | stale | conflict
    updated_at: Mapped[float] = mapped_column(Float, index=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class MatchFollow(Base):
    __tablename__ = "match_follows"
    __table_args__ = (UniqueConstraint("network", "target", "team_norm"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(100))  # channel to notify
    requested_by: Mapped[str] = mapped_column(String(100))
    team: Mapped[str] = mapped_column(String(120))
    team_norm: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[float] = mapped_column(Float)


class Ranking(Base):
    __tablename__ = "rankings"
    __table_args__ = (UniqueConstraint("source", "edition", "position"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    edition: Mapped[str] = mapped_column(String(30))
    position: Mapped[int] = mapped_column(Integer)
    team: Mapped[str] = mapped_column(String(120))
    points: Mapped[float | None] = mapped_column(Float, nullable=True)
    roster: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[float] = mapped_column(Float)
