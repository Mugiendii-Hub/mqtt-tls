#!/usr/bin/env bash
# Prints NAME=value lines for the given /mqtt secrets, for setting up a
# development machine without copying values by hand. Run over SSH:
#   (umask 077; ssh root@mqtt.nodewave.dev /root/mqtt/provision/export-env.sh \
#      MQTT_PROVISION_BOOTSTRAP_PASSWORD MQTT_PROVISION_API_KEY \
#      MQTT_RELAY_DESKTOP_PASSWORD > ~/.config/nodewave.env)
# Only names starting with MQTT_ are allowed.
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 MQTT_NAME..." >&2; exit 1; }

creds=/root/.config/infisical-nodewave.env
get() { grep -E "^$1=" "$creds" | cut -d= -f2- | tr -d "\r" || true; }
domain=$(get INFISICAL_DOMAIN); domain=${domain:-http://127.0.0.1:8070}
token=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=$(get INFISICAL_CLIENT_ID) \
        INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=$(get INFISICAL_CLIENT_SECRET) \
        infisical login --method=universal-auth --domain="$domain" --plain --silent)

for name in "$@"; do
  [[ $name =~ ^MQTT_[A-Z0-9_]+$ ]] || { echo "refusing $name" >&2; exit 1; }
  value=$(INFISICAL_TOKEN=$token infisical secrets get "$name" --plain --silent \
            --env=prod --path=/mqtt --projectId="$(get INFISICAL_PROJECT_ID)" --domain="$domain")
  [ -n "$value" ] || { echo "$name not found" >&2; exit 1; }
  echo "$name=$value"
done
