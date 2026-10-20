# mqtt

Config and runbook for the server's MQTT broker (Mosquitto 2.0, systemd
`mosquitto`). The broker itself runs from `/etc/mosquitto`; this repo is the
source of truth for what Nodewave adds to it. Device-side changes are in
[FIRMWARE.md](FIRMWARE.md).

## Addresses

| Address | For | Status |
|---|---|---|
| `157.173.107.18:1883` (plain) | field devices on old firmware | open, brute-force limited (below); closes after the firmware rollout |
| `mqtts://mqtt.nodewave.dev:8883` (TLS) | devices on new firmware, remote apps | **pending DNS**: needs `mqtt.nodewave.dev` A record → 157.173.107.18, DNS only (grey cloud) |
| `wss://mqtt-ws.nodewave.dev/mqtt` | browser apps | **pending DNS**: `mqtt-ws.nodewave.dev`, proxied; listener ready on 127.0.0.1:9083 |
| `127.0.0.1:1883` | relays and apps on this server | live |
| `127.0.0.1:1884` | ChirpStack's own broker (anonymous) | local only since 2026-10-20 |

## Logins and access

Passwords live in Infisical (self-hosted, Nodewave / prod / `/mqtt`), never in
this repo. Access rules: [`acl`](acl). A login with no rules can do nothing.

| Login | Who | Can |
|---|---|---|
| `<device-id>` | one per device (new firmware) | publish own `status`/`points`, read own `cmd`/`cfg` |
| `admin` | **legacy, shared by all devices** | device topics only (`+/status`, `+/points`, `+/cmd`, `+/cfg`) |
| `relay-mqtt` | `mqtt_relay.service` | read `+/status`, write `+/cmd` |
| `relay-flowhub` | `flowhub-relay.service` | read `+/status` `+/points`, write `+/cmd` `+/cfg` |
| `relay-desktop` | the flowhub relay run from a laptop (`flowhub_relay_local`) | same as relay-flowhub |
| `monitoring` | `/root/monitoring` mqtt-logger | read everything incl. `$SYS`, write nothing |
| `user` | unknown, never seen logging in | nothing (no rules) |

The relays read their login from `/etc/mqtt-relays/<unit>.env` (root only) via
a drop-in `/etc/systemd/system/<unit>.service.d/mqtt-login.conf`.

- New device: `./add-device.sh <id>` (creates the login, stores the password, prints it once)
- Rotate a password: run `./add-device.sh <id>` again, then send the device the new one via `<id>/cfg` (FIRMWARE.md §4)
- Change access rules: edit `acl`, then `./install.sh`

## Files

| File | Installed to | |
|---|---|---|
| `acl` | `/etc/mosquitto/acl` | access rules |
| `nodewave.conf` | `/etc/mosquitto/conf.d/nodewave.conf` | ACL, connection logging, WebSocket and TLS listeners |
| `mqtt-guard.nft` | `/etc/nftables.d/mqtt-guard.nft` (`mqtt-guard.service`) | brute-force limit |
| `install.sh` | | installs acl + conf, restarts mosquitto |

`/etc/mosquitto/mosquitto.conf` (Ubuntu's) still holds `listener 1883`,
`password_file` and `allow_anonymous false`.

Port 9001 is taken by the `sevens-system` container; the WebSocket listener
is on 9083. A listener that cannot bind makes Mosquitto refuse to start, so
check `ss -ltn` before adding one.

## Brute-force guard

Port 1883 sees thousands of password guesses a day. `mqtt-guard.service`
loads its own nftables table (it never touches Docker's rules) that drops any
IP opening more than 20 new connections a minute to 1883/8883, for one hour.
This server and Docker containers are exempt.

```bash
nft list set inet mqtt_guard blocked4                       # who is blocked now
nft delete element inet mqtt_guard blocked4 { 1.2.3.4 }      # unblock one
```

**Never enable `nftables.service`:** `/etc/nftables.conf` starts with
`flush ruleset`, which would delete Docker's firewall rules at boot.

## Monitoring

Grafana → **MQTT** dashboard (https://monitor.nodewave.dev): broker up,
clients, devices and last message per topic, logins (successful and failed),
top connecting IPs, and every message (topic, size, first 300 bytes).
Sources: `/var/log/mosquitto/mosquitto.log` (Alloy) and the mqtt-logger
service in `/root/monitoring`. Alerts: `DeviceSilent` (15 min),
`MQTTBrokerUnreachable`, `MQTTLoggerDisconnected`.

## Enabling TLS (once the DNS record exists)

1. `certbot certonly --nginx -d mqtt.nodewave.dev` (port 80 on that name answers the ACME challenge)
2. Deploy hook so renewals reach Mosquitto, `/etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh`:
   copy `fullchain.pem`/`privkey.pem` to `/etc/mosquitto/certs/mqtt.nodewave.dev/`
   (owner mosquitto, 600) and `systemctl reload mosquitto`
3. `./install.sh` (it uncomments the `#TLS` lines when the certificate is present)
4. Add `mqtt.nodewave.dev:8883` to `/root/monitoring/targets.d` (TLS check + cert expiry)
5. Move `flowhub_relay_local` (laptop) to `relay-desktop` on 8883

## Closing 1883

When the MQTT dashboard shows no `admin` logins for a week:

1. Remove `admin` from `/etc/mosquitto/passwd` and its section from `acl`
2. Bind 1883 to 127.0.0.1 (`listener 1883 127.0.0.1` in `mosquitto.conf`)
3. Update the monitoring check in `/root/monitoring/targets.d` to 8883
