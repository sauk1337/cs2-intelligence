#!/usr/bin/env bash
# Manual install on Debian/Ubuntu (no Docker). Run as root from the project root:
#   sudo ./scripts/install.sh
# Creates the `cs2intel` user, installs the backend in /opt/cs2intel, builds Eggdrop and registers
# systemd services with automatic restart.
set -euo pipefail

PREFIX=/opt/cs2intel
EGG_VERSION="${EGGDROP_VERSION:-1.10.2}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }

echo "==> System packages"
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip build-essential curl ca-certificates \
    tcl tcl-dev libssl-dev sqlite3 tzdata gettext-base

PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' || {
  echo "Python 3.12+ is required (found $PYV). On older systems use Docker or scripts/install-deps.sh --python."; exit 1; }

id cs2intel >/dev/null 2>&1 || useradd --system --create-home --home-dir "$PREFIX" --shell /usr/sbin/nologin cs2intel

echo "==> Copying the project to $PREFIX/app"
mkdir -p "$PREFIX/app"
cp -r "$SRC/backend" "$SRC/config" "$SRC/scripts" "$SRC/tests" "$SRC/eggdrop" "$SRC/requirements.txt" \
      "$SRC/requirements-dev.txt" "$SRC/pytest.ini" "$PREFIX/app/"
[ -f "$PREFIX/app/.env" ] || cp "$SRC/.env.example" "$PREFIX/app/.env"
mkdir -p "$PREFIX/app/data" "$PREFIX/app/logs" "$PREFIX/app/backups"

echo "==> Python virtual environment"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install -r "$PREFIX/app/requirements.txt"

if [ ! -x "$PREFIX/eggdrop/eggdrop" ]; then
  echo "==> Building Eggdrop $EGG_VERSION"
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/eggheads/eggdrop/archive/refs/tags/v${EGG_VERSION}.tar.gz" | tar xz -C "$tmp"
  (cd "$tmp/eggdrop-${EGG_VERSION}" && ./configure && make config && make && make install DEST="$PREFIX/eggdrop")
  rm -rf "$tmp"
fi
cp "$SRC/eggdrop/scripts/cs2intel.tcl" "$PREFIX/eggdrop/scripts/"
[ -f "$PREFIX/eggdrop/scripts/cs2intel.conf.tcl" ] || cp "$SRC/eggdrop/scripts/cs2intel.conf.tcl.example" "$PREFIX/eggdrop/scripts/cs2intel.conf.tcl"
[ -f "$PREFIX/eggdrop/eggdrop.conf" ] || cp "$SRC/eggdrop/config/eggdrop.conf.template" "$PREFIX/eggdrop/eggdrop.conf.template"
mkdir -p "$PREFIX/eggdrop/data" "$PREFIX/eggdrop/logs"

chown -R cs2intel:cs2intel "$PREFIX"
chmod 600 "$PREFIX/app/.env"

echo "==> systemd services"
cp "$SRC/deploy/systemd/cs2intel-backend.service" "$SRC/deploy/systemd/cs2intel-eggdrop.service" \
   "$SRC/deploy/systemd/cs2intel-maintenance.service" "$SRC/deploy/systemd/cs2intel-maintenance.timer" /etc/systemd/system/
systemctl daemon-reload
"$PREFIX/venv/bin/python" -m backend.main init-db --config-dir "$PREFIX/app/config" || true

cat <<EOF

Installation complete. Next steps:
 1. Edit $PREFIX/app/.env (BOT_API_TOKEN, IRC_*, optional credentials).
 2. Edit $PREFIX/app/config/settings.yaml → networks (network and channels).
 3. Generate eggdrop.conf:  sudo -u cs2intel $PREFIX/app/scripts/render-eggdrop-conf.sh
 4. Start:  systemctl enable --now cs2intel-backend cs2intel-eggdrop cs2intel-maintenance.timer
 5. First time: on IRC, /msg <bot-nick> hello  (makes you the bot owner).
 6. Check the sources: sudo -u cs2intel $PREFIX/venv/bin/python -m backend.main check
EOF
