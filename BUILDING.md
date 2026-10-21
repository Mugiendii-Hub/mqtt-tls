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
username, its client id and its topic prefix.

| Topic | Direction | QoS | Retained | Content |
|---|---|---|---|---|
| `<id>/status` | device → server | 1 | yes | `online` / `offline` (last will) or a JSON state |
| `<id>/points` | device → server | 0 or 1 | optional | measurements |
| `<id>/cmd` | server → device | 1 | no | commands |
| `<id>/cfg` | server → device | 1 | no | configuration (JSON with a `type` field) |

Known devices: `oceo2` (OCEO2 / flowhub), `rtu014` (RTU014).

Apps subscribe with wildcards (`+/status`, `+/points`) and publish to one
device (`oceo2/cmd`). Anything outside these four topic kinds is refused.

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
| `<device-id>` (e.g. `oceo2`) | one device on new firmware | own `<id>/cmd`, `<id>/cfg` | own `<id>/status`, `<id>/points` | `MQTT_DEVICE_<ID>_PASSWORD` (e.g. `MQTT_DEVICE_OCEO2_PASSWORD`) |
| `relay-mqtt` | `mqtt_relay.service` (server) | `+/status` | `+/cmd` | `MQTT_RELAY_MQTT_PASSWORD` |
| `relay-flowhub` | `flowhub-relay.service` (server) | `+/status`, `+/points` | `+/cmd`, `+/cfg` | `MQTT_RELAY_FLOWHUB_PASSWORD` |
| `relay-desktop` | flowhub relay run from a laptop | `+/status`, `+/points` | `+/cmd`, `+/cfg` | `MQTT_RELAY_DESKTOP_PASSWORD` |
| `monitoring` | Grafana mqtt-logger | everything incl. `$SYS/#` | nothing | `MQTT_MONITORING_PASSWORD` |
| `admin` | **legacy**: devices on old firmware | device topics | device topics | not in Infisical, being retired |

Device logins are created by the server team: `./add-device.sh <id>` in
`/root/mqtt` creates the login, stores the password in Infisical and shows it
once.

**A new application needs a new login.** Send the server team:
1. the username you want (e.g. `app-dashboard`),
2. exactly which topics it reads and which it writes,
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
- [ ] Handle `status` being retained: on subscribe you get the **last** state immediately, which may be old

### Python (paho-mqtt 2.x)

```python
import os, ssl
import paho.mqtt.client as mqtt

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe([("+/status", 1), ("+/points", 0)])  # re-subscribes on reconnect
    else:
        print("connect refused:", reason_code)                # bad login or not allowed

def on_message(client, userdata, msg):
    device, kind = msg.topic.split("/", 1)
    print(device, kind, msg.payload[:200])

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="app-example-laptop")
c.username_pw_set(os.environ["MQTT_USERNAME"], os.environ["MQTT_PASSWORD"])
c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)                # system CAs include ISRG Root X1
c.reconnect_delay_set(min_delay=1, max_delay=60)
c.on_connect, c.on_message = on_connect, on_message
c.connect("mqtt.nodewave.dev", 8883, keepalive=60)

# send a command to one device
c.publish("oceo2/cmd", '{"action":"read"}', qos=1)
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

client.on("connect", () => client.subscribe(["+/status", "+/points"], { qos: 1 }));
client.on("message", (topic, payload) => {
  const [device, kind] = topic.split("/");
  console.log(device, kind, payload.toString().slice(0, 200));
});
client.on("error", (err) => console.error("mqtt:", err.message)); // e.g. "Not authorized"

client.publish("oceo2/cmd", JSON.stringify({ action: "read" }), { qos: 1 });
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

- A browser app gets its **own read-only login** (e.g. reads `+/status`, writes
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
| Username = client id | the device id, e.g. `oceo2` |
| Password | per device, from the server team (`MQTT_DEVICE_<ID>_PASSWORD`) |
| Keepalive / clean session | 60 s / yes |
| Last will | `<id>/status` = `offline`, QoS 1, retained |
| On connect | publish `<id>/status` = `online`, QoS 1, retained; subscribe `<id>/cmd`, `<id>/cfg` |

Must-haves:

1. **Clock first.** Sync time (network time `AT+CTZU=1` or NTP) before connecting.
   A clock at 1980/2000 fails every TLS handshake.
2. **Store broker settings in flash** and accept new ones on `<id>/cfg`
   (`{"type":"mqtt","host":…,"port":…,"username":…,"password":…,"apply_id":…}`).
   Save them only after a successful connect with them. After 3 failures, go
   back to the last settings that worked. Report `{"cfg_applied":…}` or
   `{"cfg_failed":…}` on `<id>/status`. Ignore unknown `type` values.
   This is what makes this the last reflash.
3. **Back off on reconnect** (see §1). A device stuck in a fast reconnect
   loop blocks its own IP for an hour.
4. Publish only to its own `<id>/status` and `<id>/points`. Anything else is
   silently dropped.

Connect result codes: `0` accepted · `4`/`5` wrong username/password or not
allowed · a TLS error almost always means a wrong clock or a missing CA file.

## 6. Testing

From any computer with `mosquitto-clients`:

```bash
# watch a device (uses an app login that can read it)
mosquitto_sub -h mqtt.nodewave.dev -p 8883 --capath /etc/ssl/certs \
  -u relay-desktop -P "$MQTT_PASSWORD" -t 'oceo2/#' -v

# act as a device (device login)
curl -sO https://letsencrypt.org/certs/isrgrootx1.pem
mosquitto_pub -h mqtt.nodewave.dev -p 8883 --cafile isrgrootx1.pem \
  -u oceo2 -P "$DEVICE_PASSWORD" -i oceo2-test -t oceo2/status -m test -q 1
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
