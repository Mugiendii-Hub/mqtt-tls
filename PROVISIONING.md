# Device provisioning API

For the platform app that installs devices. Every device has its own MQTT
login (id + password), but nobody types or copies passwords: the server
creates them and keeps a copy in Infisical (Nodewave / prod / `/mqtt`,
`MQTT_DEVICE_<ID>_PASSWORD`).

Two ways to give a device its login:

| | How | Use for |
|---|---|---|
| **Zero-touch** (normal) | Flash the same firmware on every device. It boots, appears in the app as "new device, IMEI …"; you type its id and approve. The server sends the device its login; the device saves it and reboots. **The password never leaves the server and the device.** | all new devices |
| Manual | `POST /v1/devices` returns the password once; you put it on the device yourself. | testing, special cases |

| | |
|---|---|
| Base URL | `https://mqtt-api.nodewave.dev` |
| Auth | header `Authorization: Bearer <API key>` on every `/v1/` call |
| API key | Infisical, Nodewave / prod / `/mqtt`, `MQTT_PROVISION_API_KEY` |
| Format | JSON in, JSON out |
| Rate limit | 30 requests a minute per IP (burst 10); over that: `429` |

**The API key can create and delete every device login.** Keep it on your
app's **server** (environment variable / secret store). Never put it in a
browser page, a mobile app or a repo. The installer's screen talks to your
backend; your backend calls this API.

## Installing a device (zero-touch)

```
1. Device boots, no login yet. Connects with the shared first-boot login,
   publishes a claim every minute:   provision/imei-<IMEI>/up

2. Your app:  GET /v1/claims  ──▶  "IMEI 861234567890123, pending, seen 1 min ago"
   Installer types the id (temp-001), presses Approve:
              POST /v1/claims/861234567890123/approve  {"id":"temp-001"}

3. Server creates login temp-001 with a random password and sends it to that
   device only:                       provision/imei-<IMEI>/down

4. Device saves id + password in flash, reboots, connects as temp-001.
   The claim disappears from GET /v1/claims: done.
```

The IMEI is printed on the modem / module label and on the device's debug
output, so the installer can match the physical device to the claim.

### List claims: `GET /v1/claims`

```json
{"claims": [
  {"imei": "861234567890123", "status": "pending", "device_id": null,
   "first_seen": "2026-10-21T08:07:50+00:00", "last_seen": "2026-10-21T08:12:50+00:00",
   "approved_at": null, "fw": "1.1.0", "uid": "0123456789abcdef01234567"}
]}
```

`status`: `pending` (waiting for approval) or `approved` (login sent, device
not yet seen on it; normally gone within a minute). Newest first. A claim not
seen for 30 days is dropped. `fw` and `uid` (STM32 chip id) are for display.

### Approve: `POST /v1/claims/{imei}/approve`

Body `{"id":"temp-001"}` (same id rules as below). `200`:

```json
{"claim": {"imei": "861234567890123", "status": "approved", "device_id": "temp-001", ...},
 "device": {"id": "temp-001", "broker": {...}, "topics": {...}, "infisical_key": "..."}}
```

No password in the response: it goes to the device only. `404` no such claim,
`409` id already exists or claim already approved.

### Reject: `DELETE /v1/claims/{imei}`

`204`. Removes the claim (e.g. an IMEI you don't recognise). If the device is
still running it claims again within a minute.

### Moving a device to another id

Send `{"type":"reprovision"}` on `<old-id>/down/cfg` (the device forgets its
login and claims again), then `DELETE /v1/devices/<old-id>`, then approve the
new claim with the new id.

## Endpoints for device logins

### Create a device: `POST /v1/devices`

```bash
curl -X POST https://mqtt-api.nodewave.dev/v1/devices \
  -H "Authorization: Bearer $MQTT_PROVISION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"pump-01"}'
```

`201 Created`:

```json
{
  "id": "pump-01",
  "username": "pump-01",
  "client_id": "pump-01",
  "password": "x7Kq...32 characters...",
  "broker": {"host": "mqtt.nodewave.dev", "port": 8883, "tls": true,
             "ca": "ISRG Root X1 (https://letsencrypt.org/certs/isrgrootx1.pem)"},
  "topics": {"publish": "pump-01/up/#", "subscribe": "pump-01/down/#"},
  "infisical_key": "MQTT_DEVICE_PUMP_01_PASSWORD"
}
```

The device can connect immediately. Show the password to the installer and do
not store it in your app unless you protect it; it stays retrievable from
Infisical.

Id rules: lowercase letters, digits and `-`, 2–31 characters, starting with
a letter or digit. `admin`, `user`, `monitoring`, `provision`, `relay-*`,
`app-*` and `imei-*` are reserved. Ids are never reused for a different device.

### Get a new password: `POST /v1/devices/{id}/rotate`

Same response as create, with a new password. **The old password stops working
immediately**: a device still using it is disconnected at its next connect.
Rotate only when you can update the device (reflash, or `<id>/down/cfg` once
the firmware supports it).

### Remove a device: `DELETE /v1/devices/{id}`

`204 No Content`. The login and its Infisical secret are deleted; if the
device is online it is **disconnected at once** and cannot reconnect.

### List devices: `GET /v1/devices`

```json
{"devices": ["pump-01", "tank-kisumu-02"]}
```

### One device: `GET /v1/devices/{id}`

Same fields as create, **without** the password. `404` if it doesn't exist.

### Health: `GET /healthz`

No key needed. `{"ok": true}`.

## Errors

Always `{"error": "<message for a human>"}`.

| Status | Meaning |
|---|---|
| `400` | bad or reserved id, or body not JSON |
| `401` | missing or wrong API key |
| `404` | no such device / no such endpoint |
| `409` | device already exists (use rotate), or claim already approved |
| `413` | body over 1 KB |
| `429` | rate limit; wait and retry |
| `500` / `502` | server or secret-store problem; nothing half-done is left behind on create |

## Example backend call

Node.js (18+):

```js
async function createDevice(id) {
  const res = await fetch("https://mqtt-api.nodewave.dev/v1/devices", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.MQTT_PROVISION_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ id }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error);
  return body; // { id, password, broker, topics, ... }
}
```

Python:

```python
import os, requests

def create_device(device_id):
    r = requests.post("https://mqtt-api.nodewave.dev/v1/devices",
                      headers={"Authorization": f"Bearer {os.environ['MQTT_PROVISION_API_KEY']}"},
                      json={"id": device_id}, timeout=15)
    if not r.ok:
        raise RuntimeError(r.json()["error"])
    return r.json()
```

## Security of the first-boot login

Every firmware image contains the shared `provision` login
(`MQTT_PROVISION_BOOTSTRAP_PASSWORD`). Assume it leaks. With it, someone can
only publish claims (they show up in your list; reject what you don't
recognise) and read the reply for the IMEI they connect as. They can never
read device data, send commands, or see other claims. Only approve IMEIs that
belong to devices in front of you.

## Pairing a development app

A development tool (e.g. the commissioning simulator on a laptop) gets the
app keys without anyone copying them: the server team makes a one-time code,
the tool trades it for its keys once.

```bash
# on the server: make a code (valid 15 minutes, one use)
curl -s -X POST -H "Authorization: Bearer $MQTT_PROVISION_API_KEY" \
  https://mqtt-api.nodewave.dev/v1/pair-codes        # {"code":"K7QM-4XPA","expires_in":900}
```

`POST /v1/pair {"code":"K7QM-4XPA"}` (no API key) returns
`MQTT_PROVISION_API_KEY` and `MQTT_RELAY_DESKTOP_PASSWORD` once; the code is
then gone. Codes are case- and dash-insensitive; after 10 wrong codes all
open codes are dropped.

## Server side (for the server team)

- Code: `provision/` in this repo. `provision/install.sh` installs or updates it
  (`--new-key` replaces the API key; update your app's copy afterwards).
- Service: `mqtt-provision.service`, listening on 127.0.0.1:8790; logs
  (`journalctl -u mqtt-provision`) show every create/rotate/delete and every
  wrong key, with the caller's IP, never a password.
- `./add-device.sh <id>` still works on the server and does the same as create.
- Claims: `/var/lib/mqtt-provision/claims.json`. The service talks to the
  broker as `relay-provision` (`MQTT_RELAY_PROVISION_PASSWORD`).
- Firmware: `mugiendii/A7672E_-Mqtt-module-stm32`. The first-boot password goes
  in its `Core/Inc/fleet_config.h` (gitignored).
