#!/usr/bin/env bash
# Upgrades an existing Eggdrop to 1.10.2 (or EGGDROP_VERSION) keeping its configuration.
#
#   ./scripts/upgrade-eggdrop.sh /path/to/bot [binary]
#   e.g. ./scripts/upgrade-eggdrop.sh ~/cs2 cs2
#
# What it does:
#   1. Full backup of the bot directory (tar.gz next to it).
#   2. Builds the new version from the official sources (github.com/eggheads/eggdrop).
#   3. Replaces ONLY the binary, the modules and the help/language files.
#      It does NOT touch eggdrop.conf, egg.user, egg.chan, scripts/, logs/ or filesys/.
#   4. Stops the bot (SIGTERM: it saves userfile/chanfile) and starts it with the new binary.
# Options: NO_RESTART=1 (don't restart)  EGGDROP_VERSION=1.10.2
set -euo pipefail

BOT_DIR="$(cd "${1:?Usage: $0 /path/to/bot [binary]}" && pwd)"
BIN="${2:-cs2}"
VER="${EGGDROP_VERSION:-1.10.2}"
CONF="${CONF:-eggdrop.conf}"

cd "$BOT_DIR"
[ -f "$CONF" ] || { echo "Cannot find $BOT_DIR/$CONF"; exit 1; }
[ -e "$BIN" ] || { echo "Cannot find the binary $BOT_DIR/$BIN"; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "Do not run Eggdrop as root: use the bot user"; exit 1; }

for tool in gcc make curl tar; do
  command -v "$tool" >/dev/null || { echo "Missing $tool. On Debian/Ubuntu: sudo apt install build-essential curl tcl-dev libssl-dev"; exit 1; }
done
[ -n "$(find /usr/include /usr/local/include -name tcl.h 2>/dev/null | head -1)" ] || {
  echo "Tcl headers are missing: sudo apt install tcl-dev libssl-dev"; exit 1; }

echo "==> Current version: $(./"$BIN" -v 2>/dev/null | head -1 || echo unknown)"

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BOT_DIR/../$(basename "$BOT_DIR")-backup-$STAMP.tar.gz"
echo "==> Full backup in $BACKUP"
tar czf "$BACKUP" --exclude='./logs/*' -C "$BOT_DIR" .

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "==> Downloading and building Eggdrop $VER"
curl -fsSL "https://github.com/eggheads/eggdrop/archive/refs/tags/v${VER}.tar.gz" | tar xz -C "$TMP"
( cd "$TMP/eggdrop-${VER}" && ./configure >/dev/null && make config >/dev/null && make -j"$(nproc 2>/dev/null || echo 2)" >/dev/null \
  && make install DEST="$TMP/inst" >/dev/null )
NEWBIN="$TMP/inst/eggdrop-${VER}"
[ -x "$NEWBIN" ] || { echo "The build did not produce $NEWBIN"; exit 1; }
"$NEWBIN" -v | head -1

echo "==> Installing binary and modules"
cp -f "$NEWBIN" "$BOT_DIR/eggdrop-${VER}"
rm -rf "$BOT_DIR/modules-${VER}"
cp -r "$TMP/inst/modules-${VER}" "$BOT_DIR/modules-${VER}"
# eggdrop.conf's mod-path points at modules/ → link it to the new version
if [ -d "$BOT_DIR/modules" ] && [ ! -L "$BOT_DIR/modules" ]; then mv "$BOT_DIR/modules" "$BOT_DIR/modules.old-$STAMP"; fi
ln -sfn "modules-${VER}" "$BOT_DIR/modules"
for d in help language; do
  [ -d "$TMP/inst/$d" ] && cp -r "$TMP/inst/$d/." "$BOT_DIR/$d/"
done

if [ "${NO_RESTART:-0}" = "1" ]; then
  cp -f "$BOT_DIR/eggdrop-${VER}" "$BOT_DIR/$BIN.new"
  echo "Done. New binary at $BIN.new (NO_RESTART=1). To apply: stop the bot, mv $BIN.new $BIN and start it."
  exit 0
fi

echo "==> Restarting the bot"
PIDFILE=$(grep -E '^\s*set\s+pidfile' "$CONF" | sed -E 's/.*"(.*)".*/\1/' || true)
PIDFILE="${PIDFILE:-pid.$(grep -E '^\s*set\s+nick' "$CONF" | sed -E 's/.*"(.*)".*/\1/')}"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  PID=$(cat "$PIDFILE")
  kill -TERM "$PID"        # Eggdrop saves userfile and chanfile on SIGTERM
  for _ in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -0 "$PID" 2>/dev/null && { echo "The bot did not stop; check process $PID"; exit 1; }
fi
mv -f "$BOT_DIR/$BIN" "$BOT_DIR/$BIN.old-$STAMP" 2>/dev/null || true
cp -f "$BOT_DIR/eggdrop-${VER}" "$BOT_DIR/$BIN"
./"$BIN" "$CONF"
sleep 3
echo "==> Started: $(./"$BIN" -v | head -1)"
echo "If something goes wrong, restore with:  tar xzf $BACKUP -C $BOT_DIR"
