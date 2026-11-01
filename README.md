# CS2 Intelligence Bot

**A Counter-Strike 2 intelligence platform for IRC**, built on Eggdrop + a Python engine:
CS2 news, **skin market intelligence** (price history, drops, opportunity scoring, arbitrage, trends,
personal watchlists), esports data, **LIVE match tracking** and Valve update analysis — all in clean,
colourful IRC messages.

> Market indicators are informational only. **This is not financial advice** and nothing here
> guarantees any profit. A price drop does not mean an item is a good buy.

---

## Table of contents
1. [Features](#features)
2. [How it works](#how-it-works)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [IRC commands](#irc-commands)
7. [Partyline, CLI and maintenance](#partyline-cli-and-maintenance)
8. [Data sources](#data-sources)
9. [Limitations](#limitations)
10. [Testing](#testing)
11. [Troubleshooting](#troubleshooting)
12. [Project layout](#project-layout)
13. [License](#license)

---

## Features

### Skin market intelligence (core module)
- **Real, bulk market data**: Skinport `/v1/items` (~21,000 live prices) and `/v1/sales/history`
  (real sales statistics for 24h/7d/30d/90d over ~37,000 items) give a useful baseline from the very
  first start. Steam `priceoverview` is queried for watched items and opportunity candidates;
  CSFloat, Buff163 and Pricempire are optional adapters.
- **Strict item normalisation**: StatTrak™, Souvenir, every wear (FN/MW/FT/WW/BS) and every Doppler
  phase are separate items and are never mixed. Item types: weapon, knife, gloves, sticker, capsule,
  case, agent, music kit, patch, charm… Collection and rarity come from the public ByMykel/CSGO-API dataset.
- **Base currency** (EUR by default) converted with **official ECB rates**. If a rate is missing the
  price is not stored — no invented conversions.
- **Own price history**: hourly snapshots, compacted to one point per day after 7 days, kept for 400 days.
  Metrics use the bot's own history as soon as it covers the window; before that they use the platform's
  sales statistics — and every message says which basis was used.
- **Metrics**: current price, 24h/7d/30d change, 30-day mean and median, 90-day low/high, volume
  (24h/7d/30d), volume trend, **liquidity** score (0-1, logarithmic), bid/ask spread (when the source
  has buy orders), volatility, cross-platform comparison, trend (7d vs 30d and 30d vs 90d).
- **Manipulation / anomaly detection**: thin markets, outlier sales, sharp rises on low volume, very low
  prices with only 1-2 listings, prices far below other platforms, large discounts **not confirmed** on
  another platform, stale data.
- **Opportunity detector** with an explainable 0-100 score and labels
  **★ EXCELLENT · INTERESTING · WATCH · ⚠ HIGH RISK**, a risk level (LOW/MEDIUM/HIGH), the reason, the
  break-even price rise after fees and a link.
- **Arbitrage**, calculated conservatively: buyer and seller fees, undercut to sell fast, destination
  median sale price, non-cashable balances (Steam wallet), trade holds, liquidity and data age.
- **Trends**: gainers, losers, volume spikes, abnormal drops, cases, Major stickers, collections,
  low-liquidity items. Sparklines in IRC and optional SVG charts.
- **Personal watchlists** with a % threshold and/or a target price (alert when it is crossed).

### Opportunity scoring (`config/scoring.yaml`, reloadable live)
| Component | Weight | What it measures |
|---|---|---|
| Price drop | 25 | 24h/7d/30d drop, % below the 30-day median, closeness to the 90-day low |
| Volume | 15 | enough real sales and their trend |
| Liquidity | 15 | daily sales (log scale) |
| Historical trend | 15 | one-off dip in a healthy trend vs a prolonged downtrend |
| Cross-platform | 15 | how much cheaper it is than on other platforms |
| News context | 10 | stable context vs recent news/patches that add uncertainty |
| Manipulation | −20 | penalty driven by the anomaly signals |

Weights are normalised to 100. `!skinscore <skin>` shows the full, transparent breakdown.
No AI is used to predict prices — the optional LLM only summarises/translates/classifies news.

### News aggregator
- Sources: Steam News API (official Valve announcements), HLTV, Dust2.us, Dexerto, Esports Insider
  and Pley.gg (RSS; general esports feeds are filtered to CS only).
- Incremental fetching (ETag / Last-Modified), duplicate detection by normalised URL **and** by headline
  similarity across sources, 12 categories, 0-100 relevance score, "important" flag (⚡), full history.
- Optional summaries/translation into the bot language with Claude (Anthropic API) or a self-hosted
  LibreTranslate. Only news from the last 6 hours is announced (no backlog dump on startup).

### Valve update analysis
Every official patch is posted with **FACTS** (literal lines from the patch notes) and
**POSSIBLE MARKET IMPACT (speculation, not confirmed)**: new cases, drop-pool changes, trading changes,
Market changes, stickers/capsules, operations, trade-ups, map-pool changes and specific weapon changes.

### Esports & LIVE
- Liquipedia (schedule, results, tournaments, teams, players), **Valve Regional Standings** (official
  ranking), HLTV (only with authorised access), PandaScore and FACEIT (API keys).
- **Multi-source reconciliation**: matches are grouped by team pair + start time, the most reliable and
  freshest source wins (configurable priority), score **conflicts** are detected, every match carries a
  status (`ok · conflict · stale · inferred`), a confidence level and a match ID.
- An inferred state ("probably started, per schedule") or stale data is **never** announced as current.
- Events: match start, map change, map end, final result; importance filters (event tier, playoffs/finals,
  top-ranked teams) and **per-team following** (`!followmatch`).
- Adaptive polling: faster while matches are live (HLTV 15s, PandaScore 10s, FACEIT 15s, Liquipedia 60s
  due to its API rules), results every 60s, schedules/tournaments every few hours.

### Alerts & publishing
- Per channel: categories, minimum relevance/score/importance, posts-per-hour limit, quiet hours
  (urgent alerts still go through), silent mode, per-key cooldowns, global pause.
- Users and admins toggle alerts from IRC by **group** (`market`, `news`, `live`, `all`) or by single
  category; everything suppressed is logged with its reason (`!admin alerts`).

### IRC presentation
English output with mIRC colours: coloured badges (`CS2 LIVE`, `SKIN OPPORTUNITY`, `MARKET ALERT`,
`VALVE UPDATE`…), bold values, green rises / red drops, colour-coded labels, risk, liquidity and trend,
paginated replies (`!more`). Set `irc.colors: false` for plain text.

### Reliability & security
Eggdrop auto-reconnect · isolated scheduled tasks with backoff · timeouts · retries honouring
`Retry-After` · automatic pause on 401/403/anti-bot challenges (**never bypassed**) · health checks ·
rotated logs · daily SQLite backups · token + IP allow-list on the internal API · CR/LF/control-char
sanitising on both sides (no IRC command injection) · per-user anti-flood (Tcl and Python) · line byte
limits · all secrets in `.env` only.

---

## How it works

```
 IRC ──► Eggdrop (Tcl: cs2intel.tcl) ──async local HTTP──► Python backend (FastAPI + asyncio)
          │  bind pub/msg  !command                        │
          │  puthelp (Eggdrop's anti-flood queue)           ├─ Scheduler (isolated periodic tasks)
          └─ polls /api/v1/outbox every 3s ◄────────────────┤─ HTTP client (authorised outbound IP,
                                                            │   per-source rate limits, retries,
                                                            │   cache, ETag, robots.txt, block handling)
                                                            ├─ News · Market · Esports · Alerts
                                                            ├─ SQLite (WAL) or PostgreSQL
                                                            └─ optional Redis cache
```

- **Eggdrop** handles IRC: connection, reconnection, user flags/permissions and its flood-safe queue.
- **Tcl** only translates IRC events ⇄ HTTP. Every request is **asynchronous** (`http::geturl -command`),
  so scraping never blocks Eggdrop.
- **Python 3.12+** does everything else. The Tcl ⇄ Python protocol is plain UTF-8 text with a shared
  token (`X-Bot-Token`); the API listens on `127.0.0.1` (or the private Docker network).
- Automatic posts go through an **outbox**: the backend decides what to post and Eggdrop only collects
  it while connected to IRC and while its own queue is not full.
- **Multi-network**: one Eggdrop per network (each with its own `cs2intel(network)`), one shared backend.

---

## Requirements

### Server
- Linux: Debian 11/12 or Ubuntu 20.04/22.04/24.04 (other distros work, install the equivalent packages).
  A KVM VPS (Hetzner, OVH, DigitalOcean…) is fine; OpenVZ/LXC containers may not run Docker.
- ~1 GB RAM, ~2 GB disk (the SQLite database grows to a few hundred MB with a year of price history).
- Outbound HTTPS access to the data sources; no inbound ports needed (the API listens on 127.0.0.1).
- An IRC nick and channel on your network.

### Software
| Need | Packages / version | Used for |
|---|---|---|
| Eggdrop **1.10.2** (recommended, 1.9+ works) | built from source by the scripts | IRC core |
| C toolchain + Tcl 8.6 + OpenSSL headers | `build-essential tcl tcl-dev libssl-dev` | building Eggdrop, Tcl `http` package |
| Python **3.12+** | `python3 python3-venv python3-pip` (or Docker, or portable Python via `uv`) | backend |
| Python packages | `requirements.txt` (exact tested versions: `requirements-lock.txt`) | backend |
| Test tools (optional) | `requirements-dev.txt` (pytest, pytest-asyncio, respx) | test suite |
| Utilities | `curl git tar gzip rsync sqlite3 tzdata gettext-base cron` | scripts, backups, envsubst, watchdogs |
| Docker Engine + Compose v2 (optional) | `curl -fsSL https://get.docker.com \| sh` | container deployment |
| PostgreSQL / Redis (optional) | `asyncpg`, `redis` Python packages | instead of SQLite / shared cache |

Python packages (`requirements.txt`): fastapi, uvicorn[standard], python-multipart, httpx[http2,brotli],
SQLAlchemy[asyncio], aiosqlite, feedparser, beautifulsoup4, lxml, PyYAML, python-dotenv, rapidfuzz,
tzdata, anthropic (only used when `NEWS_SUMMARIZER=anthropic`).

### Install all dependencies in one go
```bash
sudo ./scripts/install-deps.sh                          # system packages
sudo ./scripts/install-deps.sh --docker --user <botuser>   # …plus Docker (optional)
./scripts/install-deps.sh --python                      # as the bot user: .venv + requirements + .env
```
`--python` uses the system Python if it is ≥ 3.12; otherwise it installs a portable Python 3.12 in
your home with [uv](https://astral.sh/uv) (no root needed). For a reproducible environment use
`.venv/bin/pip install -r requirements-lock.txt`.

---

## Installation

Pick the option that matches your setup:

| Option | When to use it |
|---|---|
| [A. Docker (new bot)](#a-docker--new-bot) | Fresh install: backend + a new Eggdrop in containers |
| [B. Existing Eggdrop, one-shot deploy](#b-existing-eggdrop-one-shot-deploy-script) | You already run an Eggdrop and want to keep its nick, users, channels and scripts |
| [C. Existing Eggdrop, manual](#c-existing-eggdrop-manual) | Same as B, step by step |
| [D. Manual install with systemd](#d-manual-install-with-systemd) | No Docker; backend + Eggdrop as system services |

First install the dependencies (see [Requirements](#requirements)), then, for every option:
```bash
git clone https://github.com/<you>/cs2-intelligence.git
cd cs2-intelligence
cp .env.example .env
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # paste into BOT_API_TOKEN
nano .env              # IRC server/nick/channel, owner, optional API keys
```

### A. Docker — new bot
```bash
mkdir -p data logs/eggdrop backups eggdrop/data
sudo chown -R 1000:1000 data logs backups eggdrop/data
docker compose up -d --build
docker compose logs -f eggdrop
```
1. When the bot joins IRC, message it from your owner nick (`EGG_OWNER`): `/msg <botnick> hello`
2. Check the sources: `docker compose exec backend python -m backend.main check`
3. Optional profiles: `--profile redis` (shared cache) and `--profile translate` (LibreTranslate).

The Docker Eggdrop has no network-specific login (e.g. QuakeNet Q auth). If your network needs one,
use option B/C with your existing bot.

### B. Existing Eggdrop: one-shot deploy script
Run as the **bot's user** (not root). It builds Eggdrop 1.10.2 while the old bot keeps running, starts
the backend (Docker if available, otherwise a Python venv), stops the old bot cleanly, moves it to
`~/<botdir>-old-DATE`, installs the new one in the same folder **with the old config, users, channels
and scripts** + the CS2 Intelligence script, starts it and adds crontab watchdogs.
```bash
sudo apt install -y build-essential curl tcl-dev libssl-dev python3-venv   # once, as root
./scripts/deploy-server.sh ~/mybot          # BOT_BIN=<binary name> if it isn't "cs2"
./scripts/deploy-server.sh --rollback       # restore the previous bot if needed
```
Set `IRC_NETWORK` / `IRC_MAIN_CHANNEL` in `.env` to match your network and channel.
If you only want to upgrade an existing Eggdrop to 1.10.2 (keeping everything):
`./scripts/upgrade-eggdrop.sh ~/mybot [binary]`.

### C. Existing Eggdrop: manual
1. Start the backend on the same server:
   - Docker: in `docker-compose.override.yml` give the backend `network_mode: host` and
     `API_HOST=127.0.0.1` and set `eggdrop` to a profile, then `docker compose up -d backend`; **or**
   - venv: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt &&
     nohup .venv/bin/python -m backend.main serve >> logs/backend-stdout.log 2>&1 &`
2. Copy `eggdrop/scripts/cs2intel.tcl` to your bot's `scripts/`, and
   `eggdrop/scripts/cs2intel.conf.tcl.example` as `scripts/cs2intel.conf.tcl` (same token as `.env`).
3. Append to your `eggdrop.conf`:
   ```tcl
   source scripts/cs2intel.conf.tcl
   source scripts/cs2intel.tcl
   ```
4. In the partyline: `.rehash` then `.chanset #yourchannel +cs2intel`.

### D. Manual install with systemd
```bash
sudo ./scripts/install.sh                                  # venv + Eggdrop 1.10.2 + systemd units
sudo nano /opt/cs2intel/app/.env
sudo -u cs2intel /opt/cs2intel/app/scripts/render-eggdrop-conf.sh
sudo systemctl enable --now cs2intel-backend cs2intel-eggdrop cs2intel-maintenance.timer
```

### After installing
```bash
curl -s 127.0.0.1:8787/health            # "status":"ok"
python -m backend.main check             # one real request to every enabled source
```
In the channel: `!version`, `!status`, `!skin ak redline ft`. Market data arrives about 1 minute after
start; the first opportunity analysis runs after ~5 minutes.

---

## Configuration

| File | What it holds |
|---|---|
| `.env` | secrets and deployment values (token, IRC, API keys, outbound IP) — never commit it |
| `config/settings.yaml` | networks/channels and their posting policy, intervals, market, alerts, retention |
| `config/sources.yaml` | every data source: enable/disable, rate limits, fees, documented authorisation |
| `config/scoring.yaml` | weights and thresholds of the opportunity engine |
| `config/local.yaml` | optional, git-ignored local overrides (same structure) |

`!admin reload` applies settings, thresholds, weights and channel policies live. Enabling a new source
or changing task intervals needs a backend restart.

### Per-channel options (`settings.yaml → networks.<network>.channels."#channel"`)
```yaml
categories: [news, valve, market, opportunity, arbitrage, trend, volume, live, results, upcoming]
min_news_relevance: 55       # 0-100, higher = fewer news posts
min_opportunity_score: 70    # only opportunities scoring at least this
min_live_importance: 40      # filters out minor matches
max_per_hour: 14
quiet_hours: "03:00-08:00"   # only urgent alerts during these hours
silent: false                # true = no automatic posts in this channel
```

### Outbound network
- `OUTBOUND_IP`: local IP all requests leave from (for servers with several IPs / an authorised IP).
- `OUTBOUND_PROXY`: optional authorised proxy.
- `HTTP_USER_AGENT`: identify yourself with a contact (Liquipedia requires it).
- Each source in `sources.yaml` has an `authorization:` field to document the access you were granted.
  IP authorisation never replaces credentials or rate limits; on 401/403/challenges the source is paused.

### Optional credentials
| Source | How to get it | Variables |
|---|---|---|
| CSFloat | csfloat.com → Profile → Developers → API key | `CSFLOAT_ENABLED=true`, `CSFLOAT_API_KEY` |
| PandaScore | app.pandascore.co → token | `PANDASCORE_ENABLED=true`, `PANDASCORE_TOKEN` |
| FACEIT | developers.faceit.com → App → API key (server side); championship/hub IDs in `sources.yaml` | `FACEIT_ENABLED=true`, `FACEIT_API_KEY` |
| HLTV | an agreement with HLTV (server IP and/or an agreed header/cookie) | `HLTV_ENABLED=true`, `HLTV_AUTH_HEADER`, `HLTV_COOKIE`, `HLTV_USER_AGENT` |
| Buff163 | authorised access only: your own account's session cookie | `BUFF_ENABLED=true`, `BUFF_COOKIE` |
| Pricempire | pricempire.com/api (paid); adjust `url/params/provider` to your plan | `PRICEMPIRE_ENABLED=true`, `PRICEMPIRE_API_KEY` |
| Claude (news summaries) | console.anthropic.com → API Keys | `NEWS_SUMMARIZER=anthropic`, `ANTHROPIC_API_KEY` |

### Database
SQLite (WAL) by default at `data/cs2intel.db`. PostgreSQL:
`DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/cs2intel` (+ `WITH_POSTGRES=true` in Docker or
`pip install asyncpg`). Tables are created automatically.

---

## IRC commands

Commands work in channels with `+cs2intel` and in private messages. Long replies are paginated: `!more`.

### General
| Command | Description |
|---|---|
| `!help [topic]` | Help by NOTICE. Topics: `general` `news` `market` `skins` `esports` `live` `economy` `alerts` |
| `!cs2` / `!about` | About the bot |
| `!status` | Sources, market, live matches and posting status |
| `!uptime` · `!version` | Backend uptime · version |
| `!more` | Next page of the previous reply |

### News
| Command | Description |
|---|---|
| `!news` / `!news latest` | Latest news |
| `!news esports` | Tournaments, teams, players, transfers |
| `!news market` | Skins, cases, Steam Market |
| `!news updates` | Valve patches, maps, operations |
| `!news major` | Major news only |

### Skins
| Command | Description |
|---|---|
| `!skin <name>` | Full sheet: price, change, median, 90d range, volume, liquidity, trend, variants |
| `!skinprice <name>` | Price on every platform |
| `!skintrend <name>` | Trend, sparkline and chart link (if enabled) |
| `!skincompare <name>` | Platforms compared + what you'd net after fees on each |
| `!skinscore <name>` | 0-100 opportunity score explained component by component |

Name search is tolerant: `!skin ak redline ft`, `!skin st awp asiimov bs`, `!skin karambit doppler fn`.
Wears `fn mw ft ww bs` · `st` = StatTrak · `souvenir` · aliases `ak deagle usp m4a1s m4 glock kara bayo`…

### Watchlist
| Command | Description |
|---|---|
| `!watch <skin> [5%] [<price]` | Watch an item (alias `!skinwatch`). Example: `!watch ak redline ft 8% <20` |
| `!unwatch <skin>` | Stop watching |
| `!watchlist` | Your list and how much each item moved |

### Market & economy
| Command | Description |
|---|---|
| `!market` / `!market top` | Most traded items |
| `!market drops` | Biggest drops vs the 30-day median |
| `!market gainers` · `!market losers` | 7d vs 30d median movers |
| `!market volume` | Volume spikes |
| `!market trending` | Items on the move |
| `!market lowliq` | Low-liquidity items |
| `!market opportunities` | Opportunities from the last 24h (label, score, risk, reason) |
| `!market arbitrage` | Cross-platform gaps that beat fees |
| `!marketstats` | Market overview |
| `!cases` · `!stickers` · `!collections` | Cases, Major stickers/capsules, collections on the move |

### Esports
| Command | Description |
|---|---|
| `!matches` / `!upcoming` | Upcoming matches (configured timezone) |
| `!results` / `!finished` | Latest results |
| `!tournaments` | Ongoing, upcoming and recent tournaments |
| `!major` | Majors, live Major matches and Major news |
| `!ranking` | Valve Regional Standings (top 15) |
| `!teams` | Top 10 teams with rosters |
| `!team <name>` | Roster, coach, ranking, next match |
| `!player <nick>` | Player profile |

### LIVE
| Command | Description |
|---|---|
| `!live` | All live CS2 matches |
| `!live <team>` | That team's live match (or its next one) |
| `!score <team>` | Current or latest score |
| `!match <team>` | Full match sheet: maps, source, confidence, sync status |
| `!matchstats <team>` | Player stats (K-D, ADR, KAST, rating, MVPs) when the source provides them |
| `!followmatch <team>` | Alerts for that team's start, maps and result in this channel |
| `!unfollowmatch <team>` | Stop following |
| `!livealerts` | LIVE alert status and followed teams |

### Automatic alerts
| Command | Description |
|---|---|
| `!alerts` | Every category ON/OFF, by group |
| `!alerts market off` | Market group: market, opportunity, arbitrage, trend, volume, watch |
| `!alerts news off` | News group: news, valve |
| `!alerts live off` | LIVE group: live, results, upcoming |
| `!alerts all off` · `!alerts all on` | Everything |
| `!alerts off <category>` · `!alerts on <category>` | A single category |

Categories: `news valve market opportunity arbitrage trend volume live results upcoming watch`.
Word order is free (`!alerts off market` = `!alerts market off`). Changing a channel requires admin;
in private, `!alerts off watch` mutes only your own watchlist alerts.

### Admin (Eggdrop flag `n` by default, configurable)
| Command | Description |
|---|---|
| `!admin status` | Database, scheduled tasks and failures |
| `!admin sources` | Health of every data source |
| `!admin alerts` | Alerts sent/suppressed in 24h with reasons |
| `!admin logs` | Latest warnings and errors |
| `!admin config` | Networks, channels, active sources, weights (no secrets) |
| `!admin pause` · `!admin resume` | Pause/resume automatic posts |
| `!admin reload` | Reload YAML configuration live |
| `!admin backup` | Database backup now |
| `!admin refresh <task>` | Run a task now: `news skinport_items skinport_history market_analysis watchlists trend_digest live_liquipedia tournaments ranking fx metadata maintenance backup` |

### Example output
```
 SKIN OPPORTUNITY  INTERESTING M4A1-S | Electrum (MW) — €1.53 · Score 77/100 · Risk MEDIUM
Change: 24h -32.3% 7d -31.7% 30d -26.1% (vs median sale of each period) · 30d median €2.07 · 90d low €1.22
Volume ► STABLE (261 sales/30d) · Liquidity MEDIUM · Trend ▲ BULLISH
Why: 32.3% below the 24h median sale (skinport); stable long-term trend; caution: 26% discount vs median not confirmed on another platform…
Source: Skinport · Not financial advice.  » https://skinport.com/item/m4a1-s-electrum-minimal-wear
```
(real Skinport data, colours removed)

### Suggested channel topic
```
CS2 | News / Skin Market / LIVE matches | !live !upcoming !news !skin <name> !market drops !watch <skin> !ranking | !help = all commands | Not financial advice
```

---

## Partyline, CLI and maintenance

**Partyline** (DCC chat / telnet): `.cs2intel` (bridge status) · `.chanset #chan +cs2intel` ·
`.chaninfo #chan` · `.rehash` (reload scripts without disconnecting).

**Backend CLI** (`python -m backend.main …`):
| Command | Description |
|---|---|
| `serve` | Run the backend (default) |
| `init-db` | Create the database tables |
| `check` | One real request to every enabled source, OK/FAIL/OFF report |
| `backup` | SQLite online backup (gzip, keeps 14) |
| `maintenance` | Compact price history and purge old data |

| Task | Automatic | Manual |
|---|---|---|
| History compaction + retention cleanup | daily | `backend.main maintenance` / `scripts/maintenance.sh` |
| SQLite backup | daily (+ systemd timer) | `backend.main backup` / `scripts/backup.sh` / `!admin backup` |
| PostgreSQL backup | — | `scripts/backup.sh` (pg_dump) |
| Logs | backend 10 MB × 7, Eggdrop daily | — |
| Health | Docker healthcheck every 30s | `curl 127.0.0.1:8787/health` · `!admin status` |

Restore SQLite: stop the backend, `gunzip -c backups/cs2intel-<date>.db.gz > data/cs2intel.db`, start it.

**Updating**: `./scripts/update.sh docker` (backup → build → tests → up) or `sudo ./scripts/update.sh`.

---

## Data sources

Verified with `python -m backend.main check`:

| Source | Status | Access | Notes |
|---|---|---|---|
| Steam News API (Valve) | ✅ working | public | official announcements and patch notes |
| HLTV RSS · Dust2.us RSS | ✅ working | public | news only |
| Dexerto · Esports Insider · Pley.gg RSS | ✅ working | public | general feeds, filtered to CS |
| Skinport API | ✅ working | public, no key | 8 requests / 5 min; real prices and sales |
| ECB exchange rates | ✅ working | public | |
| ByMykel/CSGO-API | ✅ working | public | collections/rarity metadata (no prices) |
| Liquipedia API | ✅ working | public, identifying User-Agent | 1 req/2s, `parse` 1 req/30s → LIVE ≈ 60s, no round scores |
| Valve Regional Standings | ✅ working | public GitHub | official ranking |
| Steam Community Market | ⚠️ limited | public | very strict rate limit (HTTP 429); `pricehistory` needs login (not used) |
| CSFloat | 🔑 API key | personal key | 403 without key |
| PandaScore | 🔑 token | free/paid plans | live round scores only on paid plans |
| FACEIT Data API | 🔑 API key | developers.faceit.com | per-map player stats |
| HLTV (matches/LIVE/stats) | 🔐 authorisation | authorised IP / header | Cloudflare 403 without it; parser tested with synthetic HTML |
| Buff163 | 🔐 authorised access | own session cookie | parser tested with synthetic JSON |
| Pricempire | 🔑 paid | API key | configurable format, not verified against the real API |
| Esports Charts · Dust2.us matches | ❌ not integrated | — | no public API / same backend as HLTV |
| GRID | ❌ not integrated | application required | recommended licensed alternative for round-by-round LIVE |

Respect each provider's terms of service, robots.txt and rate limits. The bot never tries to bypass
captchas, anti-bot challenges or authentication.

---

## Limitations
- **Round-by-round LIVE scores** need HLTV (authorised), paid PandaScore or GRID. With public sources only,
  LIVE is series/map level with ~60s delay (Liquipedia) and is labelled as such.
- Player stats depend on HLTV (authorised) or FACEIT.
- Without an LLM or LibreTranslate, news is shown in its original language with an extractive summary.
- The bot's own price history starts empty; until it covers 24h/7d/30d, changes are measured against
  Skinport median sales (and every message says so).
- The cheapest listing can be a unit with a worse float/pattern, which is why large discounts not
  confirmed on another platform raise the risk level.
- Platform fees in `sources.yaml` are indicative — check them periodically.

---

## Testing
```bash
pip install -r requirements-dev.txt
python -m pytest -q            # 77 tests
```
Covers: source parsers with real captured responses (`tests/fixtures`), item normalisation, prices and
currency conversion, drop detection, scoring, arbitrage, alerts (cooldown, limits, quiet hours, pause,
groups), watchlists, persistence/compaction/backup, LIVE reconciliation (conflicts, stale data, events),
the API (auth, injection, pagination, anti-flood), the HTTP client (retries, 429, blocks, ETag), safe mIRC
colour codes and **the real Tcl script running in a Tcl 8.6 interpreter** against the real backend.
Test market/match data is synthetic and lives only in temporary databases.

---

## Troubleshooting
| Symptom | Fix |
|---|---|
| The bot ignores `!` commands | Channel missing the flag: `.chanset #chan +cs2intel` |
| "The CS2 analysis service is not available" | Backend down: `curl 127.0.0.1:8787/health`, check `logs/errors.log`; same token on both sides? |
| No automatic posts | `!admin alerts` shows why (threshold, cooldown, hourly limit, quiet hours, pause, category off) |
| `steam: blocked` / 429 | Steam rate limit: lower `max_items_per_run` or raise `steam_queue_s` |
| `hltv: blocked` (403) | The IP/header is not authorised by HLTV; other sources keep working |
| `!live` lags behind | Public sources update every ~60s; add HLTV (authorised), PandaScore or FACEIT |
| `Python >= 3.12` error | Use Docker, or `scripts/deploy-server.sh` (installs Python 3.12 with `uv`, no root) |

---

## Project layout
```
cs2-intelligence/
├── backend/
│   ├── main.py              CLI: serve | init-db | check | backup | maintenance
│   ├── app.py               service wiring and scheduled tasks
│   ├── api/                 HTTP API for Eggdrop + SVG charts
│   ├── core/                config, HTTP client, scheduler, source health, cache, logging
│   ├── sources/             one adapter per source: news/ market/ esports/
│   ├── news/                aggregator, classification, relevance, summaries, patch analysis
│   ├── market/              item normalisation, ingestion, history, search, analysis
│   ├── analytics/           metrics, explainable scoring, arbitrage
│   ├── esports/             multi-source LIVE, reconciliation, events, teams, ranking
│   ├── alerts/              publisher (cooldowns, limits, quiet hours) and alert engine
│   ├── irc/                 commands and message formatting
│   └── database/            models, sessions, compaction, cleanup, backups
├── eggdrop/                 scripts/cs2intel.tcl · config/eggdrop.conf.template · Dockerfile
├── config/                  settings.yaml · sources.yaml · scoring.yaml
├── deploy/systemd/          services + maintenance timer
├── scripts/                 install-deps · install · deploy-server · upgrade-eggdrop · backup · maintenance · update
├── tests/                   test suite + captured fixtures
├── docker-compose.yml · Dockerfile · .env.example · requirements.txt · requirements-dev.txt · requirements-lock.txt
└── README.md · LICENSE
```

---

## License
Released under the [MIT License](LICENSE).

Use of third-party data sources is subject to each provider's own terms of service; this project does not
grant any rights over that data.
