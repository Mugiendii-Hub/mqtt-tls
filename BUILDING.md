# Building on the Nodewave MQTT broker

Guideline for anyone writing an application or device firmware that talks to
the Nodewave MQTT broker (Mosquitto 2.0). Updated 2026-10-21.

## 1. Where to connect

| Address | Use it for | Notes |
|---|---|---|
| `mqtts://mqtt.nodewave.dev:8883` | **device firmware, apps off the server** (laptops, cloud, phones) | TLS 1.2, Let's Encrypt certificate |
| `wss://mqtt-ws.nodewave.dev/mqtt` | **browser apps** | MQTT over WebSocket, port 443, through Cloudflare |
| `mqtt://127.0.0.1:1883` | apps running **on the server itself** | plain, never leaves the machine |
| `157.173.107.18:1883` | old device firmware only | **closing**: do not build anything new on it |

Rules:

- Always connect by **hostname**, never by IP. The IP will change one day; the name won't.
- Trust the **ISRG Root X1** CA (Let's Encrypt root), not the server certificate.
  The server certificate renews every ~60 days. Most OSes, browsers and language
  runtimes already trust ISRG Root X1. Embedded devices need the file:
  https://letsencrypt.org/certs/isrgrootx1.pem
- Connections from one IP are limited to **20 new connections a minute**. Go over
  that and the IP is blocked for an hour. Reconnect with a back-off (for example
  1 s, 2 s, 4 s … capped at 60 s), never in a tight loop.

## 2. Topics

Every device has one id: lowercase letters, digits and `-`. The id is its
username, its client id and its topic prefix. Each device owns two branches:

```
<id>/up/...     device → server   (the device publishes, apps read)
<id>/down/...   server → device   (apps publish, the device reads)
```

**Any topic name can be added under `up/` or `down/` with no change on the
server.** A device can only publish under its own `up/` and read its own
`down/`; it can never touch another device's topics.

Standard topics (use these names where they fit; add your own alongside):

| Topic | QoS | Retained | Content |
|---|---|---|---|
| `<id>/up/status` | 1 | yes | `online` / `offline` (last will) or a JSON state |
| `<id>/up/points` | 0 or 1 | optional | measurements / capability report |
| `<id>/down/cmd` | 1 | no | commands |
| `<id>/down/cfg` | 1 | no | configuration (JSON with a `type` field) |
| *e.g.* `<id>/up/alarms`, `<id>/up/gps`, `<id>/down/schedule` | your choice | your choice | new topics: just use them |

Naming: lowercase, words joined with `-` or `_`, no spaces, no leading `/`,
no `+` or `#` in names. Sub-levels are fine (`<id>/up/sensors/temp`).

How apps subscribe:

| Subscribe to | Gets |
|---|---|
| `+/up/#` | everything from every device, including topics added later |
| `+/up/alarms` | one kind from every device |
| `dev01/up/#` | everything from one device |

Apps send to one device at a time: publish `dev01/down/cmd`. Devices subscribe
once to `<id>/down/#` and ignore topics they don't understand.

**Old firmware** (shared `admin` login) still uses `<id>/status`, `<id>/points`,
`<id>/cmd`, `<id>/cfg` until it is reflashed. Don't build anything new on
those names.

**The broker drops what a login may not do, without telling you.** A publish to
a topic the login cannot write never arrives, and a subscribe to a topic it
cannot read gets no messages. Nothing errors. When "nothing arrives", check the
login's rights in §3 first.

## 3. Logins

Every program gets its **own** login. Never share a login between two programs
or two devices. Passwords are in Infisical (self-hosted), project **Nodewave**,
environment **prod**, folder **`/mqtt`**. Never put them in code, a repo or chat.

| Username | For | Can read | Can write | Infisical key |
|---|---|---|---|---|
| `<device-id>` (e.g. `dev01`) | one device on new firmware | own `<id>/down/#` | own `<id>/up/#` | `MQTT_DEVICE_<ID>_PASSWORD` (e.g. `MQTT_DEVICE_DEV01_PASSWORD`) |
| `relay-mqtt` | `mqtt_relay.service` (server) | `+/up/#` (+ old `+/status`) | `+/down/#` (+ old `+/cmd`) | `MQTT_RELAY_MQTT_PASSWORD` |
| `relay-flowhub` | `flowhub-relay.service` (server) | `+/up/#` (+ old `+/status`, `+/points`) | `+/down/#` (+ old `+/cmd`, `+/cfg`) | `MQTT_RELAY_FLOWHUB_PASSWORD` |
| `relay-desktop` | flowhub relay run from a laptop | same as relay-flowhub | same as relay-flowhub | `MQTT_RELAY_DESKTOP_PASSWORD` |
| `monitoring` | Grafana mqtt-logger | everything incl. `$SYS/#` | nothing | `MQTT_MONITORING_PASSWORD` |
| `admin` | **legacy**: devices on old firmware | old topics only | old topics only | not in Infisical, being retired |

The "old" rights exist only until the last old-firmware device is reflashed.

Device logins are created when the device is installed: the platform app
calls the provisioning API with the new id and gets the password back once
(`PROVISIONING.md`). The server team can do the same with
`./add-device.sh <id>`. Either way the password is also kept in Infisical.

**A new application needs a new login.** Send the server team:
1. the username you want (e.g. `app-dashboard`),
2. which topics it reads and writes (e.g. reads `+/up/alarms`, writes nothing;
   or reads `+/up/#`, writes `+/down/cmd`). Ask for no more than it needs,
3. where it runs (server, cloud, browser).

They add it to the access rules (`acl`), create the password and store it in
Infisical under `MQTT_<NAME>_PASSWORD`. A login with no rules can do nothing.

## 4. Building an application

### Checklist

- [ ] Own login, password read from the environment / Infisical at start-up
- [ ] Client id unique and stable, e.g. `<app>-<host>` (two clients with the same id kick each other off)
- [ ] TLS (`8883` or `wss`) unless running on the server itself
- [ ] Keepalive 60 s, automatic reconnect with back-off
- [ ] Re-subscribe after every reconnect (or use a persistent session)
- [ ] Commands to devices: QoS 1, not retained
- [ ] Ignore topics you don't know (devices may add new `up/` topics at any time)
- [ ] Handle `status` being retained: on subscribe you get the **last** state immediately, which may be old

### Python (paho-mqtt 2.x)

```python
import os, ssl
import paho.mqtt.client as mqtt

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe("+/up/#", qos=1)                    # re-subscribes on reconnect
    else:
        print("connect refused:", reason_code)                # bad login or not allowed

def on_message(client, userdata, msg):
    device, _up, kind = msg.topic.split("/", 2)              # dev01/up/alarms
    print(device, kind, msg.payload[:200])

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="app-example-laptop")
c.username_pw_set(os.environ["MQTT_USERNAME"], os.environ["MQTT_PASSWORD"])
c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)                # system CAs include ISRG Root X1
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect, c.on_message = on_connect, on_message
c.connect("mqtt.nodewave.dev", 8883, keepalive=60)

# send a command to one device
c.publish("dev01/down/cmd", '{"action":"read"}', qos=1)
c.loop_forever()
```

On the server itself: drop `tls_set` and connect to `127.0.0.1`, `1883`.

### Node.js (mqtt.js 5.x)

```js
import mqtt from "mqtt";

const client = mqtt.connect("mqtts://mqtt.nodewave.dev:8883", {
  clientId: "app-example-server",
  username: process.env.MQTT_USERNAME,
  password: process.env.MQTT_PASSWORD,
  keepalive: 60,
  reconnectPeriod: 5000,
});

client.on("connect", () => client.subscribe("+/up/#", { qos: 1 }));
client.on("message", (topic, payload) => {
  const [device, , kind] = topic.split("/");                 // dev01/up/alarms
  console.log(device, kind, payload.toString().slice(0, 200));
});
client.on("error", (err) => console.error("mqtt:", err.message)); // e.g. "Not authorized"

client.publish("dev01/down/cmd", JSON.stringify({ action: "read" }), { qos: 1 });
```

### Browser (mqtt.js over WebSocket)

```js
const client = mqtt.connect("wss://mqtt-ws.nodewave.dev/mqtt", {
  clientId: "web-" + crypto.randomUUID().slice(0, 8),
  username: loginFromYourBackend.username,
  password: loginFromYourBackend.password,
});
```

**Anything shipped to a browser is public.** Whoever opens the page can read
the login from it. So:

- A browser app gets its **own read-only login** (e.g. reads `+/up/status`, writes
  nothing), never a relay login.
- If the browser must **send commands**, send them to your backend over HTTPS.
  The backend checks who the user is and publishes with its own login.
- Better still, have your backend hand out the login only to signed-in users.

## 5. Building device firmware

Full AT command walkthrough for the SIMCom A7672: `FIRMWARE.md` in `/root/mqtt`.
Summary:

| Setting | Value |
|---|---|
| Host / port | `mqtt.nodewave.dev` / `8883` |
| TLS | 1.2, verify the server, SNI on, CA file `isrgrootx1.pem` only |
| Username = client id | the device id, e.g. `dev01` |
| Password | per device, from the platform app when the device is installed (`MQTT_DEVICE_<ID>_PASSWORD`) |
| Keepalive / clean session | 60 s / yes |
| Last will | `<id>/up/status` = `offline`, QoS 1, retained |
| On connect | publish `<id>/up/status` = `online`, QoS 1, retained; subscribe `<id>/down/#` |

Must-haves:

1. **Clock first.** Sync time (network time `AT+CTZU=1` or NTP) before connecting.
   A clock at 1980/2000 fails every TLS handshake.
2. **Store broker settings in flash** and accept new ones on `<id>/down/cfg`
   (`{"type":"mqtt","host":…,"port":…,"username":…,"password":…,"apply_id":…}`).
   Save them only after a successful connect with them. After 3 failures, go
   back to the last settings that worked. Report `{"cfg_applied":…}` or
   `{"cfg_failed":…}` on `<id>/up/status`. Ignore unknown `type` values.
   This is what makes this the last reflash.
3. **Back off on reconnect** (see §1). A device stuck in a fast reconnect
   loop blocks its own IP for an hour.
4. Publish only under its own `<id>/up/`. Anything else is silently dropped.
   New sensors or events get a new `up/` topic. No server change needed.
5. Subscribe once to `<id>/down/#` and ignore `down/` topics it doesn't know.

Connect result codes: `0` accepted · `4`/`5` wrong username/password or not
allowed · a TLS error almost always means a wrong clock or a missing CA file.

## 6. Testing

From any computer with `mosquitto-clients`:

```bash
# watch a device (uses an app login that can read it)
mosquitto_sub -h mqtt.nodewave.dev -p 8883 --capath /etc/ssl/certs \
  -u relay-desktop -P "$MQTT_PASSWORD" -t 'dev01/#' -v

# act as a device (device login)
curl -sO https://letsencrypt.org/certs/isrgrootx1.pem
mosquitto_pub -h mqtt.nodewave.dev -p 8883 --cafile isrgrootx1.pem \
  -u dev01 -P "$DEVICE_PASSWORD" -i dev01-test -t dev01/up/status -m test -q 1
```

(On macOS use `--cafile isrgrootx1.pem` for both; `/etc/ssl/certs` is Linux.)

Grafana → **MQTT** dashboard (https://monitor.nodewave.dev) shows every login
(successful and failed), every message (topic, size, first 300 bytes) and the
last message per device within seconds. Check it first when something doesn't
arrive. It shows whether the login was refused or the message never came.

## 7. Before going live

- [ ] Uses its own login and nobody else's
- [ ] Connects by hostname over TLS (or `127.0.0.1` on the server)
- [ ] Survives a broker restart: reconnects and re-subscribes by itself
- [ ] Shows up in Grafana → MQTT with the expected username and topics
- [ ] (Firmware) passes the bench test in `FIRMWARE.md` §6, including recovery from a bad `cfg`
