#!/usr/bin/env bash
# Creates (or rotates) a device's own MQTT login.
#   ./add-device.sh <device-id>
# Username = device id = topic prefix (see FIRMWARE.md). The password is stored
# in Infisical (Nodewave / prod / /mqtt, MQTT_DEVICE_<ID>_PASSWORD) and shown
# once here for flashing. The acl's `pattern` rules already cover new devices.
set -euo pipefail
id=${1:?usage: ./add-device.sh <device-id>}
[[ $id =~ ^[a-z0-9][a-z0-9-]{1,30}$ ]] || { echo "device id: lowercase letters, digits, '-' (2-31 chars)"; exit 1; }
case $id in admin|user|monitoring|relay-*) echo "'$id' is reserved"; exit 1;; esac

creds=/root/.config/infisical-nodewave.env
get() { grep -E "^$1=" "$creds" | cut -d= -f2- | tr -d "\r" || true; }
domain=$(get INFISICAL_DOMAIN); domain=${domain:-http://127.0.0.1:8070}
token=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=$(get INFISICAL_CLIENT_ID) \
        INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=$(get INFISICAL_CLIENT_SECRET) \
        infisical login --method=universal-auth --domain="$domain" --plain --silent)

pw=$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)
key="MQTT_DEVICE_$(echo "$id" | tr 'a-z-' 'A-Z_')_PASSWORD"
tmp=$(umask 077; mktemp)
trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT
echo "$key=$pw" > "$tmp"
INFISICAL_TOKEN=$token infisical secrets set --file="$tmp" --env=prod --path=/mqtt \
  --projectId="$(get INFISICAL_PROJECT_ID)" --domain="$domain" >/dev/null

mosquitto_passwd -b /etc/mosquitto/passwd "$id" "$pw"
chown mosquitto:mosquitto /etc/mosquitto/passwd && chmod 600 /etc/mosquitto/passwd
systemctl reload mosquitto

echo "Device login ready:   username = client id = $id"
echo "Topics:               publish $id/up/<anything>   subscribe $id/down/#"
echo "Password (also in Infisical /mqtt $key):"
echo "  $pw"
echo "Clear your terminal after copying it."
