#!/usr/bin/env bash
# =============================================================================
#  Install everything CS2 Intelligence needs on Debian / Ubuntu.
#
#    sudo ./scripts/install-deps.sh               # system packages only
#    sudo ./scripts/install-deps.sh --docker      # + Docker Engine and Compose v2
#    sudo ./scripts/install-deps.sh --docker --user eggdrop   # + add that user to the docker group
#
#  Then, as the bot user (not root), create the Python environment:
#    ./scripts/install-deps.sh --python           # venv in .venv with requirements.txt
#  (if the system Python is older than 3.12, a portable Python 3.12 is installed with uv in $HOME)
# =============================================================================
set -euo pipefail

WANT_DOCKER=0; WANT_PYTHON=0; DOCKER_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) WANT_DOCKER=1 ;;
    --python) WANT_PYTHON=1 ;;
    --user) DOCKER_USER="${2:?--user needs a user name}"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$WANT_PYTHON" = "1" ]; then
  [ "$(id -u)" -ne 0 ] || { echo "Run --python as the bot user, not root"; exit 1; }
  cd "$PROJECT"
  PY=""
  for p in python3.13 python3.12 python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then PY=$p; break; fi
  done
  if [ -n "$PY" ]; then
    "$PY" -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  else
    echo "System Python < 3.12: installing a portable Python 3.12 with uv (no root)"
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    uv python install 3.12
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  fi
  .venv/bin/python -c 'import sys; print("Python", sys.version.split()[0], "ready in .venv")'
  mkdir -p data logs backups
  [ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created .env from .env.example: edit it now."; }
  exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo) to install system packages"; exit 1; }
command -v apt-get >/dev/null || { echo "This script supports Debian/Ubuntu (apt). Install the packages listed in README manually."; exit 1; }

echo "==> System packages"
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git tar gzip rsync \
  build-essential tcl tcl-dev libssl-dev \
  python3 python3-venv python3-pip python3-dev \
  sqlite3 tzdata gettext-base cron

if [ "$WANT_DOCKER" = "1" ]; then
  echo "==> Docker Engine + Compose v2"
  if ! command -v docker >/dev/null; then
    curl -fsSL https://get.docker.com | sh
  fi
  docker compose version
  if [ -n "$DOCKER_USER" ]; then
    usermod -aG docker "$DOCKER_USER"
    echo "User $DOCKER_USER added to the docker group (log out and back in to apply)."
  fi
fi

echo
echo "Done. Versions:"
echo "  gcc:    $(gcc -dumpversion)"
echo "  tcl:    $(echo 'puts [info patchlevel]' | tclsh)"
echo "  python: $(python3 --version 2>&1)"
command -v docker >/dev/null && echo "  docker: $(docker --version)"
echo
echo "Next: as the bot user run  ./scripts/install-deps.sh --python   (or use Docker), then see README → Installation."
