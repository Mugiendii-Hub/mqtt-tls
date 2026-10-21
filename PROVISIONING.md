# Device provisioning API

For the platform app that installs devices. You enter a device id; the app
calls this API; the API creates the device's MQTT login and returns its
password **once**. The password is also kept in Infisical (Nodewave / prod /
`/mqtt`, `MQTT_DEVICE_<ID>_PASSWORD`).

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

## Installing a device

```
installer enters id ──▶ your backend ──POST /v1/devices──▶ provisioning API
                                                                  │ creates login, stores password
installer sees password ◀── your backend ◀── id + password ◀──────┘
        │
        └─▶ put DEVICE_ID + MQTT_PASSWORD in the firmware's device_config.h, flash
```

## Endpoints

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
a letter or digit. `admin`, `user`, `monitoring`, `relay-*` and `app-*` are
reserved. Ids are never reused for a different device.

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
| `409` | device already exists (use rotate) |
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

## Server side (for the server team)

- Code: `provision/` in this repo. `provision/install.sh` installs or updates it
  (`--new-key` replaces the API key; update your app's copy afterwards).
- Service: `mqtt-provision.service`, listening on 127.0.0.1:8790; logs
  (`journalctl -u mqtt-provision`) show every create/rotate/delete and every
  wrong key, with the caller's IP, never a password.
- `./add-device.sh <id>` still works on the server and does the same as create.
