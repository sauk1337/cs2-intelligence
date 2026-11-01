#!/bin/sh
# Generates eggdrop.conf and cs2intel.conf.tcl from environment variables and runs Eggdrop in the foreground.
set -eu
cd /opt/eggdrop

: "${IRC_NICK:=CS2Intel}"
: "${IRC_ALTNICK:=CS2Intel_}"
: "${IRC_REALNAME:=CS2 Intelligence Bot}"
: "${IRC_NETWORK_NAME:=Libera.Chat}"
: "${IRC_NET_TYPE:=Libera}"
: "${IRC_SERVER:=irc.libera.chat}"
: "${IRC_PORT:=+6697}"
: "${IRC_SERVER_PASS:=}"
: "${IRC_SASL_USER:=}"
: "${IRC_SASL_PASS:=}"
: "${IRC_CHANNELS:=#cs2}"
: "${IRC_NETWORK:=minet}"
: "${EGG_OWNER:=admin}"
: "${EGG_ADMIN:=admin <admin@example.org>}"
: "${EGG_USERNAME:=cs2intel}"
: "${EGG_LISTEN_PORT:=}"
: "${BOT_TIMEZONE:=Europe/Madrid}"
: "${BACKEND_URL:=http://backend:8787}"
: "${BOT_API_TOKEN:?BOT_API_TOKEN is missing}"
export IRC_NICK IRC_ALTNICK IRC_REALNAME IRC_NETWORK_NAME IRC_NET_TYPE IRC_SERVER IRC_PORT IRC_SERVER_PASS \
       IRC_SASL_USER IRC_SASL_PASS IRC_CHANNELS EGG_OWNER EGG_ADMIN EGG_USERNAME EGG_LISTEN_PORT BOT_TIMEZONE

# Only OUR variables are substituted: Tcl variables ($chan, $::...) must stay untouched
VARS='${IRC_NICK} ${IRC_ALTNICK} ${IRC_REALNAME} ${IRC_NETWORK_NAME} ${IRC_NET_TYPE} ${IRC_SERVER} ${IRC_PORT}
${IRC_SERVER_PASS} ${IRC_SASL_USER} ${IRC_SASL_PASS} ${IRC_CHANNELS} ${EGG_OWNER} ${EGG_ADMIN} ${EGG_USERNAME}
${EGG_LISTEN_PORT} ${BOT_TIMEZONE}'
envsubst "$VARS" < eggdrop.conf.template > eggdrop.conf
chmod 600 eggdrop.conf

# Script settings (no shell expansion inside the token)
{
  printf 'set cs2intel(api_url) {%s}\n' "$BACKEND_URL"
  printf 'set cs2intel(token) {%s}\n' "$BOT_API_TOKEN"
  printf 'set cs2intel(network) {%s}\n' "$IRC_NETWORK"
  printf 'set cs2intel(admin_flags) {%s}\n' "${EGG_ADMIN_FLAGS:-n|n}"
} > scripts/cs2intel.conf.tcl
chmod 600 scripts/cs2intel.conf.tcl

if [ ! -f data/cs2intel.user ]; then
  echo "First start: creating the userfile. Then /msg ${IRC_NICK} hello from your nick (${EGG_OWNER})."
  exec ./eggdrop -n -m eggdrop.conf
fi
exec ./eggdrop -n eggdrop.conf
