#!/usr/bin/env bash
# Starts Eggdrop in the foreground (used by systemd). On the first start it creates the userfile.
set -euo pipefail
cd "${PREFIX:-/opt/cs2intel}/eggdrop"
if [ ! -f data/cs2intel.user ]; then
  exec ./eggdrop -n -m eggdrop.conf
fi
exec ./eggdrop -n eggdrop.conf
