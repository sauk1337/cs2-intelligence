#!/usr/bin/env bash
# Database backup (SQLite online backup or pg_dump) + configuration.
#   Docker:  docker compose exec backend python -m backend.main backup
#   Manual:  ./scripts/backup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -f .env ] && { set -a; . ./.env; set +a; }
DEST="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
PY="${PYTHON:-$( [ -x /opt/cs2intel/venv/bin/python ] && echo /opt/cs2intel/venv/bin/python || echo python3)}"

if [[ "${DATABASE_URL:-}" == postgresql* ]]; then
  PGURL="${DATABASE_URL/postgresql+asyncpg/postgresql}"
  pg_dump "$PGURL" | gzip > "$DEST/cs2intel-pg-$STAMP.sql.gz"
  echo "Backup PostgreSQL: $DEST/cs2intel-pg-$STAMP.sql.gz"
else
  "$PY" -m backend.main backup
fi
tar czf "$DEST/cs2intel-config-$STAMP.tar.gz" config eggdrop/scripts/cs2intel.tcl 2>/dev/null || true
# keep 14 configuration copies
ls -1t "$DEST"/cs2intel-config-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
