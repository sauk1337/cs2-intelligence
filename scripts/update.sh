#!/usr/bin/env bash
# Update: backup → new code → dependencies → tests → restart.
#   Docker:  ./scripts/update.sh docker
#   Manual:  sudo ./scripts/update.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
MODE="${1:-manual}"
if [ -d .git ]; then git pull --ff-only; fi
if [ "$MODE" = "docker" ]; then
  docker compose exec -T backend python -m backend.main backup || true
  docker compose build
  docker compose run --rm --no-deps backend python -m pytest -q
  docker compose up -d
  docker compose ps
  exit 0
fi
PREFIX=/opt/cs2intel
sudo -u cs2intel "$PREFIX/venv/bin/python" -m backend.main backup --config-dir "$PREFIX/app/config" || true
rsync -a --exclude data --exclude logs --exclude backups --exclude .env --exclude 'config/local.yaml' \
      backend config scripts tests eggdrop requirements.txt requirements-dev.txt pytest.ini "$PREFIX/app/"
cp eggdrop/scripts/cs2intel.tcl "$PREFIX/eggdrop/scripts/"
"$PREFIX/venv/bin/pip" install -r "$PREFIX/app/requirements-dev.txt"
(cd "$PREFIX/app" && sudo -u cs2intel "$PREFIX/venv/bin/python" -m pytest -q)
chown -R cs2intel:cs2intel "$PREFIX"
systemctl restart cs2intel-backend
# Eggdrop can reload the script without leaving IRC with .rehash from the partyline;
# restarting the service causes a reconnect.
systemctl restart cs2intel-eggdrop
systemctl --no-pager status cs2intel-backend cs2intel-eggdrop | head -20
