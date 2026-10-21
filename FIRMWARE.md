# Device firmware update: secure MQTT

What every field device must change so the broker can stop accepting plain,
shared-password connections. One firmware release covers all of it; after
that, broker changes (new server, new password) need **no reflash**.

| | Today | After this update |
|---|---|---|
| Broker address | `157.173.107.18:1883` (IP, hard-coded) | `mqtt.nodewave.dev:8883` (hostname) |
| Encryption | none: password and data readable on the mobile network | TLS 1.2, server certificate verified |
| Login | `admin`, shared by every device | one login per device: username = device id |
| Client id | `0000023`, `A7672Client`, ... | the device id |
| Changing any of the above later | reflash on site | `<device>/cfg` message over MQTT |

Server status: per-device logins, the access rules and TLS on
`mqtt.nodewave.dev:8883` are **live** (since 2026-10-21). Plain 1883 stays open
until the last device has moved.

---

## 1. Device id = username = client id = topic prefix

Pick one id per device and use it everywhere. Lowercase letters, digits and
`-` only. The existing topic prefixes are the ids:

| Device | Id | Topics |
|---|---|---|
| OCEO2 (flowhub) | `oceo2` | `oceo2/status`, `oceo2/points` (publish) · `oceo2/cmd`, `oceo2/cfg` (subscribe) |
| RTU014 | `rtu014` | `rtu014/status` (publish) · `rtu014/cmd` (subscribe) |

The broker enforces this (`acl`): a device's login can **only** publish its own
`status`/`points` and read its own `cmd`/`cfg`. Publishing anywhere else is
silently dropped, so a typo in a topic shows up as "server never receives it".

Get the device's password from the server team (`./add-device.sh <id>` on the
server; it is stored in Infisical, Nodewave / prod / `/mqtt`). Never reuse one
password on two devices.

## 2. Settings

| Setting | Value |
|---|---|
| Host | `mqtt.nodewave.dev` (**hostname, not the IP**) |
| Port | `8883` |
| TLS | 1.2, verify the server certificate |
| CA certificate | **ISRG Root X1** (Let's Encrypt root, valid until 2035). File: `isrgrootx1.pem` from https://letsencrypt.org/certs/isrgrootx1.pem |
| Username / client id | the device id, e.g. `oceo2` |
| Password | per device (from the server team) |
| Keepalive | 60 s |
| Clean session | yes |
| Last will | topic `<id>/status`, payload `offline`, QoS 1, retained |
| On connect | publish `<id>/status` = `online`, QoS 1, retained |

Load **only the root CA**, never the server's own certificate: the server
certificate renews every ~60 days and the device must not care.

**The device clock must be right.** TLS checks certificate dates; a modem that
boots at 1980-01-01 rejects every certificate. Sync time before connecting
(network time, or NTP).

## 3. SIMCom A7672 (A76xx) AT sequence

Check each command against the *A76XX Series MQTT(S) AT Command Manual* for
your module firmware revision; parameter numbering differs slightly between
revisions.

```text
// --- once per device (or at every boot; it is idempotent) ---------------
AT+CTZU=1                                 // take time from the network
AT+CCLK?                                  // confirm the clock is not 1980/2000
AT+CCERTDOWN="isrgrootx1.pem",<length>    // then send the PEM file, exactly <length> bytes
AT+CCERTLIST                              // isrgrootx1.pem should be listed

// --- TLS context 0 --------------------------------------------------------
AT+CSSLCFG="sslversion",0,3               // 3 = TLS 1.2
AT+CSSLCFG="authmode",0,1                 // 1 = verify the server
AT+CSSLCFG="cacert",0,"isrgrootx1.pem"
AT+CSSLCFG="enableSNI",0,1

// --- MQTT client 0 over TLS ---------------------------------------------
AT+CMQTTSTART
AT+CMQTTACCQ=0,"oceo2",1                  // client id = device id; 1 = TLS
AT+CMQTTSSLCFG=0,0                        // client 0 uses TLS context 0

// last will: <id>/status = offline (retained)
AT+CMQTTWILLTOPIC=0,12                    // then send: oceo2/status
AT+CMQTTWILLMSG=0,7,1                     // then send: offline   (QoS 1)

AT+CMQTTCONNECT=0,"tcp://mqtt.nodewave.dev:8883",60,1,"oceo2","<password>"
// expect: +CMQTTCONNECT: 0,0   (0 = accepted)

// subscribe to commands and config
AT+CMQTTSUBTOPIC=0,9,1                    // then send: oceo2/cmd
AT+CMQTTSUBTOPIC=0,9,1                    // then send: oceo2/cfg
AT+CMQTTSUB=0

// publish (example: status online, retained)
AT+CMQTTTOPIC=0,12                        // then send: oceo2/status
AT+CMQTTPAYLOAD=0,6                       // then send: online
AT+CMQTTPUB=0,1,60,1                      // QoS 1, timeout 60 s, retained
```

The `tcp://` prefix is correct even for TLS on the A76xx; TLS comes from
`AT+CMQTTACCQ=...,1` plus `AT+CMQTTSSLCFG`.

Common connect results: `0` accepted · `4`/`5` wrong username/password or not
allowed (check the id and password) · TLS/handshake errors almost always mean
a wrong clock or a missing/wrong CA file.

## 4. Remote configuration (so this is the last reflash)

The device keeps its broker settings in flash and accepts new ones on
`<id>/cfg`:

```json
{
  "type": "mqtt",
  "host": "mqtt.nodewave.dev",
  "port": 8883,
  "username": "oceo2",
  "password": "<new password>",
  "apply_id": "2026-11-01-rotate"
}
```

Rules, so a bad message can never strand a device:

1. Ignore `type` values you do not understand (the flowhub relay already uses
   `<id>/cfg` for other config; this `type` keeps them apart).
2. Try the new settings. Only if connecting with them **succeeds**, save them
   to flash and publish `<id>/status` = `{"cfg_applied":"<apply_id>"}`.
3. If they fail 3 times, go back to the last settings that worked and publish
   `{"cfg_failed":"<apply_id>"}`.
4. Keep the CA file across updates; allow replacing it via the same mechanism
   (`"type": "ca"`, PEM in the payload) for the day ISRG Root X1 is retired.

## 5. Topics and payloads (unchanged)

| Topic | Direction | QoS | Retained |
|---|---|---|---|
| `<id>/status` | device → server | 1 | yes (online/offline, last state) |
| `<id>/points` | device → server | 0 or 1 | optional |
| `<id>/cmd` | server → device | 1 | no |
| `<id>/cfg` | server → device | 1 | no |

Every message is visible in Grafana → **MQTT** dashboard (message log, last
message per device) within seconds, which makes bench testing easy.

## 6. Test before flashing the fleet

On the bench, with the new firmware:

1. Device connects: Grafana → MQTT → "Who connected" shows `user=oceo2` (not `admin`).
2. Its `status`/`points` appear in the message log and the Devices table.
3. Send a command from the relay/app; the device acts on it.
4. Pull the antenna or power: `<id>/status` turns `offline` (last will) and the
   `DeviceSilent` alert fires after 15 min.
5. Send a `<id>/cfg` with a **wrong** password: the device must recover on its own (rule 3).

Equivalent check from any computer:

```bash
curl -sO https://letsencrypt.org/certs/isrgrootx1.pem
mosquitto_pub -h mqtt.nodewave.dev -p 8883 --cafile isrgrootx1.pem \
  -u oceo2 -P '<password>' -i oceo2-test -t oceo2/status -m test -q 1
```

## 7. Rollout

1. Server team: `./add-device.sh <id>` for each device; hand over the passwords.
2. Flash one device, run the bench test, leave it in the field for a few days.
3. Flash the rest. The MQTT dashboard's "logins" panel shows `admin` logins
   falling to zero.
4. When no device has logged in as `admin` for a week: the server team removes
   the `admin` login and closes plain port 1883 (README, "Closing 1883").
