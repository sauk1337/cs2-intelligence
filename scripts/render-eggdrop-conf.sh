#!/usr/bin/env bash
# Generates eggdrop.conf and cs2intel.conf.tcl from .env (manual install).
set -euo pipefail
PREFIX="${PREFIX:-/opt/cs2intel}"
ENV_FILE="${ENV_FILE:-$PREFIX/app/.env}"
set -a; . "$ENV_FILE"; set +a
cd "$PREFIX/eggdrop"
VARS='${IRC_NICK} ${IRC_ALTNICK} ${IRC_REALNAME} ${IRC_NETWORK_NAME} ${IRC_NET_TYPE} ${IRC_SERVER} ${IRC_PORT}
${IRC_SERVER_PASS} ${IRC_SASL_USER} ${IRC_SASL_PASS} ${IRC_CHANNELS} ${EGG_OWNER} ${EGG_ADMIN} ${EGG_USERNAME}
${EGG_LISTEN_PORT} ${BOT_TIMEZONE}'
export EGG_USERNAME="${EGG_USERNAME:-cs2intel}" EGG_LISTEN_PORT="${EGG_LISTEN_PORT:-}"
envsubst "$VARS" < eggdrop.conf.template > eggdrop.conf
{
  printf 'set cs2intel(api_url) {http://127.0.0.1:%s}\n' "${API_PORT:-8787}"
  printf 'set cs2intel(token) {%s}\n' "$BOT_API_TOKEN"
  printf 'set cs2intel(network) {%s}\n' "${IRC_NETWORK:-minet}"
  printf 'set cs2intel(admin_flags) {%s}\n' "${EGG_ADMIN_FLAGS:-n|n}"
} > scripts/cs2intel.conf.tcl
chmod 600 eggdrop.conf scripts/cs2intel.conf.tcl
echo "Generated $PREFIX/eggdrop/eggdrop.conf and scripts/cs2intel.conf.tcl"
