#!/usr/bin/env bash
# certbot deploy hook: hands renewed mqtt.nodewave.dev certificates to Mosquitto.
# Installed to /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh. certbot runs
# every deploy hook for every renewed certificate, so skip the others.
set -euo pipefail
name=mqtt.nodewave.dev
[ "${RENEWED_LINEAGE:-}" = "/etc/letsencrypt/live/$name" ] || exit 0
dir=/etc/mosquitto/certs/$name
install -d -m 750 -o mosquitto -g mosquitto "$dir"
install -m 600 -o mosquitto -g mosquitto "$RENEWED_LINEAGE/fullchain.pem" "$dir/fullchain.pem"
install -m 600 -o mosquitto -g mosquitto "$RENEWED_LINEAGE/privkey.pem" "$dir/privkey.pem"
# Reload, not restart: Mosquitto rereads certificates without dropping clients
systemctl reload mosquitto
