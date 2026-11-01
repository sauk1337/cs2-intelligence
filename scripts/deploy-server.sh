#!/usr/bin/env bash
# =============================================================================
#  Deploy onto a server running an existing Eggdrop (run as the bot user, not root).
#
#  ./scripts/deploy-server.sh [CURRENT_BOT_DIR]       (default ~/cs2; BOT_BIN=binary name, default cs2)
#
#  1. Builds Eggdrop 1.10.2 (the old bot stays connected meanwhile)
#  2. Starts the Python backend (Docker if available; otherwise a venv + cron watchdog)
#  3. Stops the old bot (SIGTERM: it saves userfile/chanfile) and moves it to ~/cs2-old-DATE
#  4. Installs the new one in the same directory with the old bot's config, users, channels and
#     scripts + the CS2 Intelligence script, and starts it
#  5. Adds autostart and watchdog entries to crontab
#  Undo with: ./scripts/deploy-server.sh --rollback
# =============================================================================
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
VER="${EGGDROP_VERSION:-1.10.2}"
BIN="${BOT_BIN:-cs2}"
CONF="eggdrop.conf"
STAMP=$(date +%Y%m%d-%H%M%S)
log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*"; exit 1; }

# ---------------------------------------------------------------- rollback
if [ "${1:-}" = "--rollback" ]; then
  BOT="${2:-$HOME/cs2}"
  OLD=$(ls -d "$HOME"/cs2-old-* 2>/dev/null | sort | tail -1)
  [ -n "$OLD" ] || die "There is no ~/cs2-old-* to restore"
  [ -f "$BOT/pid.egg" ] && kill -TERM "$(cat "$BOT/pid.egg")" 2>/dev/null || true
  sleep 5
  mv "$BOT" "$BOT-failed-$STAMP"
  mv "$OLD" "$BOT"
  cd "$BOT" && ./"$BIN" "$CONF"
  echo "Old bot restored from $OLD"
  exit 0
fi

BOT="$(cd "${1:-$HOME/cs2}" && pwd)" || die "The bot directory does not exist"
[ "$(id -u)" -ne 0 ] || die "Run as the bot user, not as root"
[ -f "$BOT/$CONF" ] || die "Cannot find $BOT/$CONF"
[ -f "$PROJECT/.env" ] || die "Missing $PROJECT/.env"
cd "$PROJECT"
set -a; . ./.env; set +a
[ -n "${BOT_API_TOKEN:-}" ] || die "BOT_API_TOKEN is empty in .env"

# ------------------------------------------------------------ checks
log "Checking requirements"
MISSING=""
for t in gcc make curl tar; do command -v "$t" >/dev/null || MISSING="$MISSING $t"; done
[ -n "$(find /usr/include /usr/local/include -name tcl.h 2>/dev/null | head -1)" ] || MISSING="$MISSING tcl-dev"
[ -z "$MISSING" ] || die "Missing:$MISSING  →  sudo apt install build-essential curl tcl-dev libssl-dev python3-venv"
echo "tcl: $(echo 'puts [info patchlevel]' | tclsh 2>/dev/null || echo '?')  ·  current bot: $BOT"

# ---------------------------------------------------------- 1) build 1.10.2
log "Building Eggdrop $VER (the old bot keeps running)"
BUILD="$HOME/.cache/eggdrop-build"
if [ -x "$BUILD/inst/eggdrop-${VER}" ]; then
  echo "already built before: reusing $BUILD/inst"
else
  rm -rf "$BUILD"; mkdir -p "$BUILD"
  curl -fsSL "https://github.com/eggheads/eggdrop/archive/refs/tags/v${VER}.tar.gz" | tar xz -C "$BUILD"
  ( cd "$BUILD/eggdrop-${VER}" && ./configure >/dev/null && make config >/dev/null \
    && make -j"$(nproc 2>/dev/null || echo 2)" >/dev/null && make install DEST="$BUILD/inst" >/dev/null ) \
    || die "Build failed (check $BUILD/eggdrop-${VER})"
fi
"$BUILD/inst/eggdrop" -v | head -1

# ------------------------------------------------------------ 2) backend
log "Starting the Python backend"
BACKEND_MODE=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  mkdir -p data logs backups
  docker compose up -d --build
  BACKEND_MODE=docker
else
  echo "Docker not available for this user: using a Python venv"
  PY=""
  for p in python3.13 python3.12 python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then PY=$p; break; fi
  done
  if [ -n "$PY" ]; then
    [ -x .venv/bin/python ] || "$PY" -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
  else
    # Old system (Python < 3.12): portable Python 3.12 via uv, no root, inside $HOME
    echo "System Python < 3.12: installing Python 3.12 with uv in $HOME/.local (no root)"
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    command -v uv >/dev/null || die "Could not install uv (https://astral.sh/uv)"
    uv python install 3.12
    rm -rf .venv
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -q -r requirements.txt
  fi
  .venv/bin/python -c 'import sys; assert sys.version_info >= (3,12); print("python", sys.version.split()[0])'
  mkdir -p data logs backups
  .venv/bin/python -m backend.main init-db >/dev/null
  pkill -u "$(id -u)" -f "backend.main serve" 2>/dev/null || true
  nohup .venv/bin/python -m backend.main serve >> logs/backend-stdout.log 2>&1 &
  echo $! > data/backend.pid
  BACKEND_MODE=venv
fi
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:${API_PORT:-8787}/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:${API_PORT:-8787}/health" >/dev/null || die "The backend is not responding on 127.0.0.1:${API_PORT:-8787} (see logs/)"
echo "backend OK ($BACKEND_MODE)"

# ------------------------------------------------------ 3) stop the old bot
log "Stopping the old bot"
PIDFILE="$BOT/$(grep -E '^\s*set\s+pidfile' "$BOT/$CONF" | sed -E 's/.*"(.*)".*/\1/')"
[ -f "$PIDFILE" ] || PIDFILE="$BOT/pid.egg"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  PID=$(cat "$PIDFILE")
  kill -TERM "$PID"
  for i in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -0 "$PID" 2>/dev/null && { kill -KILL "$PID"; echo "forced (SIGKILL)"; }
  echo "stopped (pid $PID)"
else
  echo "it was not running"
fi
# remove the old crontab watchdog so it does not revive the old bot during the switch
crontab -l 2>/dev/null | grep -v -E "botchk|$BOT" | crontab - 2>/dev/null || true

OLD="$HOME/cs2-old-$STAMP"
mv "$BOT" "$OLD"
echo "old bot moved to $OLD"

# ------------------------------------------------- 4) install the new bot
log "Installing Eggdrop $VER in $BOT with the old bot's configuration"
cp -a "$BUILD/inst" "$BOT"
cd "$BOT"
cp -a "$OLD/$CONF" "$OLD"/egg.user "$OLD"/egg.chan .
[ -f "$OLD/eggdrop-basic.conf" ] && cp -a "$OLD/eggdrop-basic.conf" .
# the old bot's own scripts (stock scripts keep their new version)
for f in "$OLD"/scripts/*; do
  name=$(basename "$f")
  [ -e "scripts/$name" ] && cmp -s "$f" "scripts/$name" && continue
  case "$name" in alltools.tcl|compat.tcl|action.fix.tcl|dccwhois.tcl|getops.tcl|klined.tcl|notes2.tcl|quotepong.tcl|autobotchk|botchk|weed|CONTENTS|cmd_resolve.tcl|userinfo.tcl)
    [ -e "scripts/$name" ] && continue ;;
  esac
  cp -a "$f" scripts/
done
[ -d "$OLD/filesys" ] && cp -a "$OLD/filesys" .
[ -f "$OLD/text/motd" ] && cp -a "$OLD/text/motd" text/motd
mkdir -p logs

# CS2 Intelligence script
cp "$PROJECT/eggdrop/scripts/cs2intel.tcl" scripts/
cat > scripts/cs2intel.conf.tcl <<EOF
# generated by deploy-server.sh ($STAMP)
set cs2intel(api_url)       "http://127.0.0.1:${API_PORT:-8787}"
set cs2intel(token)         "${BOT_API_TOKEN}"
set cs2intel(network)       "${IRC_NETWORK:-minet}"
set cs2intel(admin_flags)   "${EGG_ADMIN_FLAGS:-n|n}"
set cs2intel(require_flag)  1
utimer 15 { catch { channel set ${IRC_MAIN_CHANNEL:-#cs2} +cs2intel } }
EOF
chmod 600 scripts/cs2intel.conf.tcl "$CONF"
grep -q "cs2intel.tcl" "$CONF" || printf '\n# --- CS2 Intelligence ---\nsource scripts/cs2intel.conf.tcl\nsource scripts/cs2intel.tcl\n' >> "$CONF"

cp -f "eggdrop-${VER}" "$BIN"
chmod +x "$BIN"

log "Starting the new bot"
./"$BIN" "$CONF"
sleep 5
NEWPID=$(cat "$(grep -E '^\s*set\s+pidfile' "$CONF" | sed -E 's/.*"(.*)".*/\1/')" 2>/dev/null || echo "")
[ -n "$NEWPID" ] && kill -0 "$NEWPID" 2>/dev/null || die "The bot did not start: see $BOT/logs/eggdrop.log  (undo: $PROJECT/scripts/deploy-server.sh --rollback)"
echo "bot running (pid $NEWPID)"

# ---------------------------------------------------- 5) autostart
log "Watchdog and autostart (crontab)"
PIDREL=$(grep -E '^\s*set\s+pidfile' "$CONF" | sed -E 's/.*"(.*)".*/\1/')
EGGLINE="*/10 * * * * cd $BOT && { [ -f $PIDREL ] && kill -0 \$(cat $PIDREL) 2>/dev/null; } || ./$BIN $CONF >/dev/null 2>&1"
NEW_CRON=$( (crontab -l 2>/dev/null | grep -v -E "cs2intel-watch|$BOT &&"; echo "$EGGLINE # cs2intel-watch-egg") )
if [ "$BACKEND_MODE" = "venv" ]; then
  BELINE="*/5 * * * * cd $PROJECT && curl -fsS -m 10 http://127.0.0.1:${API_PORT:-8787}/health >/dev/null 2>&1 || nohup .venv/bin/python -m backend.main serve >> logs/backend-stdout.log 2>&1 &"
  NEW_CRON=$(printf '%s\n%s # cs2intel-watch-backend\n@reboot cd %s && nohup .venv/bin/python -m backend.main serve >> logs/backend-stdout.log 2>&1 & # cs2intel-watch-boot\n' \
    "$NEW_CRON" "$BELINE" "$PROJECT")
fi
echo "$NEW_CRON" | crontab -
crontab -l | grep cs2intel-watch

sleep 20
log "Last lines of the log"
tail -n 15 logs/eggdrop.log || true
cat <<EOF

DONE.
  · New bot:       $BOT (Eggdrop $VER, binary ./$BIN)
  · Old bot:       $OLD
  · Backend:       $BACKEND_MODE  →  curl 127.0.0.1:${API_PORT:-8787}/health
  · Try in ${IRC_MAIN_CHANNEL:-#cs2}:   !version  !status  !skin ak redline ft
  · Undo:          $PROJECT/scripts/deploy-server.sh --rollback
EOF
