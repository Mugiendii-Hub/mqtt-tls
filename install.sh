#!/usr/bin/env bash
# Installs the broker config from this repo and restarts what needs it.
#   ./install.sh          ACL + conf.d, restart mosquitto
# ACL-only edits just need: install acl, then `systemctl reload mosquitto`.
set -euo pipefail
cd "$(dirname "$0")"
install -m 600 -o mosquitto -g mosquitto acl /etc/mosquitto/acl
conf=$(cat nodewave.conf)
# Turn the TLS listener on once its certificate is in place
if [ -f /etc/mosquitto/certs/mqtt.nodewave.dev/fullchain.pem ]; then
  conf=$(printf '%s\n' "$conf" | sed 's/^#TLS //')
fi
printf '%s\n' "$conf" | install -m 644 /dev/stdin /etc/mosquitto/conf.d/nodewave.conf
systemctl restart mosquitto
systemctl is-active mosquitto
