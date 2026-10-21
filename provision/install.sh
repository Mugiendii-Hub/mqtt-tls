#!/usr/bin/env bash
# Installs the provisioning API (PROVISIONING.md) and restarts it.
#   provision/install.sh             install / update
#   provision/install.sh --new-key   also replace the API key (old key stops working)
# Secrets live in Infisical (Nodewave / prod / /mqtt) and are created on the
# first run:
#   MQTT_PROVISION_API_KEY             API key for the platform app
#   MQTT_RELAY_PROVISION_PASSWORD      this service's broker login (relay-provision)
#   MQTT_PROVISION_BOOTSTRAP_PASSWORD  shared first-boot login (provision), goes in firmware
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
tmp=$(umask 077; mktemp)
trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT

# secret NAME [--new]: print the secret, creating it if missing (or --new)
secret() {
  local v
  v=$(inf secrets get "$1" --plain --silent 2>/dev/null || true)
  if [ -z "$v" ] || [ "${2:-}" = "--new" ]; then
    v=$(openssl rand -hex 32 | cut -c1-${3:-64})
    echo "$1=$v" > "$tmp"
    inf secrets set --file="$tmp" >/dev/null
    echo "New $1 stored in Infisical /mqtt" >&2
  fi
  printf '%s' "$v"
}

key=$(secret MQTT_PROVISION_API_KEY "$([ "${1:-}" = "--new-key" ] && echo --new)")
relay_pw=$(secret MQTT_RELAY_PROVISION_PASSWORD "" 32)
boot_pw=$(secret MQTT_PROVISION_BOOTSTRAP_PASSWORD "" 32)

# Broker logins (the acl already has their rules)
for login in "relay-provision:$relay_pw" "provision:$boot_pw"; do
  mosquitto_passwd -b /etc/mosquitto/passwd "${login%%:*}" "${login#*:}"
done
chown mosquitto:mosquitto /etc/mosquitto/passwd && chmod 600 /etc/mosquitto/passwd
install -m 600 -o mosquitto -g mosquitto ../acl /etc/mosquitto/acl
systemctl reload mosquitto

install -d -m 700 /etc/mqtt-provision
( umask 077
  { echo "# provisioning service (source: Infisical Nodewave /mqtt)"
    echo "MQTT_PROVISION_API_KEY=$key"
    echo "MQTT_RELAY_PROVISION_PASSWORD=$relay_pw"; } > /etc/mqtt-provision/env )

lib=/usr/local/lib/mqtt-provision
[ -x $lib/venv/bin/python ] || python3 -m venv $lib/venv
$lib/venv/bin/pip install -q --disable-pip-version-check paho-mqtt==2.1.0
install -m 755 provision.py $lib/provision.py
install -m 644 mqtt-provision.service /etc/systemd/system/mqtt-provision.service
systemctl daemon-reload
systemctl enable --now mqtt-provision >/dev/null 2>&1
systemctl restart mqtt-provision
sleep 2
systemctl is-active mqtt-provision
