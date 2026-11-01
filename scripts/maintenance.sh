#!/usr/bin/env bash
# Maintenance: compacts price history, purges old data, optimises SQLite and makes a backup.
# (The backend already does this daily; this script is for cron/systemd or manual runs.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -f .env ] && { set -a; . ./.env; set +a; }
PY="${PYTHON:-$( [ -x /opt/cs2intel/venv/bin/python ] && echo /opt/cs2intel/venv/bin/python || echo python3)}"
"$PY" -m backend.main maintenance
DB="${DATA_DIR:-$ROOT/data}/cs2intel.db"
if [ -z "${DATABASE_URL:-}" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null; then
  sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA optimize;"
fi
"$ROOT/scripts/backup.sh"
# backend logs are rotated by the backend itself (10 MB x 7). Here Eggdrop logs older than 30 days are deleted.
find "$ROOT/logs" -type f -name '*.log.*' -mtime +30 -delete 2>/dev/null || true
