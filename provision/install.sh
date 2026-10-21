#!/usr/bin/env bash
# Installs the provisioning API (PROVISIONING.md) and restarts it.
#   provision/install.sh             install / update
#   provision/install.sh --new-key   also replace the API key (old key stops working)
# The API key lives in Infisical (Nodewave / prod / /mqtt, MQTT_PROVISION_API_KEY);
# it is created on the first run and copied to /etc/mqtt-provision/env.
set -euo pipefail
cd "$(dirname "$0")"

creds=/root/.config/infisical-nodewave.env
get() { grep -E "^$1=" "$creds" | cut -d= -f2- | tr -d "\r" || true; }
domain=$(get INFISICAL_DOMAIN); domain=${domain:-http://127.0.0.1:8070}
token=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=$(get INFISICAL_CLIENT_ID) \
        INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=$(get INFISICAL_CLIENT_SECRET) \
        infisical login --method=universal-auth --domain="$domain" --plain --silent)
inf() { INFISICAL_TOKEN=$token infisical "$@" --env=prod --path=/mqtt \
          --projectId="$(get INFISICAL_PROJECT_ID)" --domain="$domain"; }

key=$(inf secrets get MQTT_PROVISION_API_KEY --plain --silent 2>/dev/null || true)
if [ -z "$key" ] || [ "${1:-}" = "--new-key" ]; then
  key=$(openssl rand -hex 32)
  tmp=$(umask 077; mktemp)
  trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT
  echo "MQTT_PROVISION_API_KEY=$key" > "$tmp"
  inf secrets set --file="$tmp" >/dev/null
  echo "New API key stored in Infisical /mqtt MQTT_PROVISION_API_KEY"
fi

install -d -m 700 /etc/mqtt-provision
( umask 077
  { echo "# provisioning API key (source: Infisical Nodewave /mqtt MQTT_PROVISION_API_KEY)"
    echo "MQTT_PROVISION_API_KEY=$key"; } > /etc/mqtt-provision/env )

install -D -m 755 provision.py /usr/local/lib/mqtt-provision/provision.py
install -m 644 mqtt-provision.service /etc/systemd/system/mqtt-provision.service
systemctl daemon-reload
systemctl enable --now mqtt-provision >/dev/null 2>&1
systemctl restart mqtt-provision
sleep 1
systemctl is-active mqtt-provision
