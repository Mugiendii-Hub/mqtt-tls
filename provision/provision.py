#!/usr/bin/env python3
"""MQTT device provisioning API (see PROVISIONING.md).

Lets a platform app hosted elsewhere create, rotate and delete device logins
on this broker: the same thing ./add-device.sh does, over HTTPS. Listens on
127.0.0.1:8790; nginx serves it as https://mqtt-api.nodewave.dev.

Zero-touch: new firmware boots without a login, connects with the shared
`provision` login as client id imei-<IMEI> and publishes a claim on
provision/imei-<IMEI>/up. Claims are listed by GET /v1/claims; approving one
with an id creates that device's login and sends it the id and password on
provision/imei-<IMEI>/down (again on every claim until the device shows up
on its new login). Nobody ever sees that password.

Every request (except /healthz) needs `Authorization: Bearer <API key>`, the
key being MQTT_PROVISION_API_KEY in Infisical (Nodewave / prod / /mqtt).
Passwords are returned once and stored in Infisical as
MQTT_DEVICE_<ID>_PASSWORD; they are never logged.
"""
import hmac
import json
import logging
import os
import re
import secrets
import string
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

LISTEN = ("127.0.0.1", 8790)
PASSWD = "/etc/mosquitto/passwd"
API_KEY = os.environ["MQTT_PROVISION_API_KEY"]

INF_BASE = os.environ.get("INFISICAL_DOMAIN") or "http://127.0.0.1:8070"
INF_CLIENT_ID = os.environ["INFISICAL_CLIENT_ID"]
INF_CLIENT_SECRET = os.environ["INFISICAL_CLIENT_SECRET"]
INF_PROJECT = os.environ["INFISICAL_PROJECT_ID"]
INF_ENV, INF_PATH = "prod", "/mqtt"

# The service's own broker login (local, plain: never leaves the machine)
MQTT_USER = "relay-provision"
MQTT_PASSWORD = os.environ["MQTT_RELAY_PROVISION_PASSWORD"]

STATE = "/var/lib/mqtt-provision/claims.json"
CLAIM_CID = re.compile(r"^imei-([0-9]{15})$")
MAX_PENDING = 500                  # ignore new claims beyond this (spam guard)
CLAIM_TTL = 30 * 86400             # forget claims not seen for 30 days

# Same rules as add-device.sh: id = username = client id = topic prefix
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
RESERVED = {"admin", "user", "monitoring", "provision"}
# server-side logins, application logins, first-boot client ids
RESERVED_PREFIXES = ("relay-", "app-", "imei-")

BROKER = {"host": "mqtt.nodewave.dev", "port": 8883, "tls": True,
          "ca": "ISRG Root X1 (https://letsencrypt.org/certs/isrgrootx1.pem)"}

log = logging.getLogger("mqtt-provision")
# One change to the password file at a time
lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ---- Infisical (REST, universal auth) -------------------------------------

_token = {"value": None, "expires": 0.0}


def _infisical(method, path, body=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + _infisical_token()
    req = urllib.request.Request(
        INF_BASE + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None


def _infisical_token():
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    status, data = _infisical("POST", "/api/v1/auth/universal-auth/login",
                              {"clientId": INF_CLIENT_ID,
                               "clientSecret": INF_CLIENT_SECRET}, auth=False)
    if status != 200:
        raise ApiError(502, "secret store login failed")
    _token["value"] = data["accessToken"]
    _token["expires"] = time.time() + data.get("expiresIn", 600) - 60
    return _token["value"]


def _secret_body(value=None):
    body = {"workspaceId": INF_PROJECT, "environment": INF_ENV,
            "secretPath": INF_PATH, "type": "shared"}
    if value is not None:
        body["secretValue"] = value
    return body


def secret_key(device_id):
    return "MQTT_DEVICE_" + device_id.upper().replace("-", "_") + "_PASSWORD"


def store_password(device_id, password):
    path = "/api/v3/secrets/raw/" + secret_key(device_id)
    status, _ = _infisical("PATCH", path, _secret_body(password))
    if status == 404:
        status, _ = _infisical("POST", path, _secret_body(password))
    if status != 200:
        raise ApiError(502, "could not store the password in the secret store")


def read_password(device_id):
    status, data = _infisical(
        "GET", "/api/v3/secrets/raw/" + secret_key(device_id)
        + f"?workspaceId={INF_PROJECT}&environment={INF_ENV}&secretPath={INF_PATH}")
    if status != 200:
        raise ApiError(502, "could not read the password from the secret store")
    return data["secret"]["secretValue"]


def delete_password(device_id):
    status, _ = _infisical("DELETE", "/api/v3/secrets/raw/" + secret_key(device_id),
                           _secret_body())
    if status not in (200, 404):
        raise ApiError(502, "could not delete the password from the secret store")


# ---- Mosquitto password file ----------------------------------------------

def broker_logins():
    with open(PASSWD) as f:
        return {line.split(":", 1)[0] for line in f if ":" in line}


def device_ids():
    return sorted(u for u in broker_logins()
                  if u not in RESERVED and not u.startswith(RESERVED_PREFIXES))


def _run(*cmd):
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def set_login(device_id, password):
    _run("mosquitto_passwd", "-b", PASSWD, device_id, password)
    _finish_passwd_change()


def delete_login(device_id):
    _run("mosquitto_passwd", "-D", PASSWD, device_id)
    _finish_passwd_change()


def _finish_passwd_change():
    os.chmod(PASSWD, 0o600)
    _run("chown", "mosquitto:mosquitto", PASSWD)
    # Reload rereads the password file without dropping connected clients
    _run("systemctl", "reload", "mosquitto")


# ---- Operations ------------------------------------------------------------

def check_id(device_id):
    if not isinstance(device_id, str) or not ID_RE.match(device_id):
        raise ApiError(400, "id: lowercase letters, digits and '-', 2-31 characters, "
                            "starting with a letter or digit")
    if device_id in RESERVED or device_id.startswith(RESERVED_PREFIXES):
        raise ApiError(400, f"'{device_id}' is reserved")


def new_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def device_info(device_id, password=None):
    info = {"id": device_id, "username": device_id, "client_id": device_id,
            "broker": BROKER,
            "topics": {"publish": f"{device_id}/up/#",
                       "subscribe": f"{device_id}/down/#"},
            "infisical_key": secret_key(device_id)}
    if password is not None:
        info["password"] = password
    return info


def _create_login(device_id):
    """New login with a new password; caller holds the lock."""
    if device_id in broker_logins():
        raise ApiError(409, f"device '{device_id}' already exists; "
                            "use rotate for a new password")
    password = new_password()
    store_password(device_id, password)
    try:
        set_login(device_id, password)
    except Exception:
        delete_password(device_id)
        raise
    return password


def create(device_id):
    check_id(device_id)
    with lock:
        password = _create_login(device_id)
    return device_info(device_id, password)


def rotate(device_id):
    check_id(device_id)
    with lock:
        if device_id not in broker_logins():
            raise ApiError(404, f"device '{device_id}' does not exist")
        password = new_password()
        store_password(device_id, password)
        set_login(device_id, password)
    return device_info(device_id, password)


def delete(device_id):
    check_id(device_id)
    with lock:
        if device_id not in broker_logins():
            raise ApiError(404, f"device '{device_id}' does not exist")
        delete_login(device_id)
        delete_password(device_id)


# ---- Zero-touch claims -----------------------------------------------------
# claims.json: {"imei-<IMEI>": {"imei", "first_seen", "last_seen", "status":
# "pending"|"approved", "device_id", "approved_at", "fw", "uid"}}. A claim is
# removed once its device has published on its new login.

claims = {}
mqttc = None


def _now():
    return time.time()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def load_claims():
    global claims
    try:
        with open(STATE) as f:
            claims = json.load(f)
    except FileNotFoundError:
        claims = {}


def save_claims():
    cutoff = _now() - CLAIM_TTL
    for cid in [c for c, v in claims.items() if v["last_seen"] < cutoff]:
        del claims[cid]
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(claims, f)
    os.replace(tmp, STATE)


def claim_view(cid):
    c = claims[cid]
    return {"imei": c["imei"], "status": c["status"], "device_id": c.get("device_id"),
            "first_seen": _iso(c["first_seen"]), "last_seen": _iso(c["last_seen"]),
            "approved_at": _iso(c.get("approved_at")),
            "fw": c.get("fw"), "uid": c.get("uid")}


def send_login(cid):
    """Send an approved claim its id and password; caller holds the lock."""
    device_id = claims[cid]["device_id"]
    payload = json.dumps({"id": device_id, "password": read_password(device_id),
                          "host": BROKER["host"], "port": BROKER["port"]})
    mqttc.publish(f"provision/{cid}/down", payload, qos=1)
    log.info("sent login %s to %s", device_id, cid)


def on_claim(cid, payload):
    if not CLAIM_CID.match(cid):
        return
    try:
        body = json.loads(payload[:512])
        body = body if isinstance(body, dict) else {}
    except ValueError:
        body = {}
    with lock:
        c = claims.get(cid)
        if c is None:
            if sum(v["status"] == "pending" for v in claims.values()) >= MAX_PENDING:
                log.warning("claim from %s ignored: %d claims pending", cid, MAX_PENDING)
                return
            c = claims[cid] = {"imei": cid[5:], "first_seen": _now(), "status": "pending"}
            log.info("new claim %s", cid)
        c["last_seen"] = _now()
        # Display only; never trusted for anything
        for key, pattern in (("fw", r"^[\w.+-]{1,32}$"), ("uid", r"^[0-9a-fA-F]{24}$")):
            if isinstance(body.get(key), str) and re.match(pattern, body[key]):
                c[key] = body[key]
        if c["status"] == "approved":
            try:
                send_login(cid)
            except ApiError as e:
                log.error("could not send login to %s: %s", cid, e.message)
        save_claims()


def on_device_status(device_id):
    with lock:
        done = [c for c, v in claims.items()
                if v["status"] == "approved" and v.get("device_id") == device_id]
        for cid in done:
            del claims[cid]
            log.info("provisioned %s as %s", cid, device_id)
        if done:
            save_claims()


def approve(imei, device_id):
    cid = "imei-" + imei
    check_id(device_id)
    with lock:
        c = claims.get(cid)
        if c is None:
            raise ApiError(404, f"no claim from IMEI {imei}")
        if c["status"] != "pending":
            raise ApiError(409, f"IMEI {imei} is already approved as '{c['device_id']}'")
        _create_login(device_id)
        c.update(status="approved", device_id=device_id, approved_at=_now())
        save_claims()
        send_login(cid)
    return {"claim": claim_view(cid), "device": device_info(device_id)}


def reject(imei):
    with lock:
        if claims.pop("imei-" + imei, None) is None:
            raise ApiError(404, f"no claim from IMEI {imei}")
        save_claims()


def start_mqtt():
    global mqttc
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-provision")
    mqttc.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    mqttc.reconnect_delay_set(min_delay=2, max_delay=60)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe([("provision/+/up", 1), ("+/up/status", 0)])
            log.info("connected to the broker")
        else:
            log.error("broker refused login: %s", reason_code)

    def on_message(client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if parts[0] == "provision" and len(parts) == 3 and parts[2] == "up":
                on_claim(parts[1], msg.payload)
            elif len(parts) == 3 and parts[1:] == ["up", "status"]:
                on_device_status(parts[0])
        except Exception:
            log.exception("handling %s failed", msg.topic)

    mqttc.on_connect, mqttc.on_message = on_connect, on_message
    mqttc.connect_async("127.0.0.1", 1883, keepalive=60)
    mqttc.loop_start()


# ---- HTTP ------------------------------------------------------------------

DEVICE_PATH = re.compile(r"^/v1/devices/([^/]+)(/rotate)?$")
CLAIM_PATH = re.compile(r"^/v1/claims/([0-9]{15})(/approve)?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "mqtt-provision"
    sys_version = ""

    def _send(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _client(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _authorised(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and hmac.compare_digest(
            auth[7:].encode(), API_KEY.encode())

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024:
            raise ApiError(413, "body too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "body must be JSON")

    def _handle(self, method):
        if method == "GET" and self.path == "/healthz":
            return self._send(200, {"ok": True})
        if not self._authorised():
            log.warning("unauthorised %s %s from %s", method, self.path, self._client())
            return self._send(401, {"error": "missing or wrong API key"})
        try:
            m = DEVICE_PATH.match(self.path)
            c = CLAIM_PATH.match(self.path)
            if method == "GET" and self.path == "/v1/claims":
                with lock:
                    out = sorted((claim_view(k) for k in claims),
                                 key=lambda v: v["last_seen"], reverse=True)
                return self._send(200, {"claims": out})
            if c and method == "POST" and c.group(2):
                body = self._json_body()
                result = approve(c.group(1),
                                 body.get("id") if isinstance(body, dict) else None)
                log.info("approved IMEI %s as %s (from %s)", c.group(1),
                         result["device"]["id"], self._client())
                return self._send(200, result)
            if c and method == "DELETE" and not c.group(2):
                reject(c.group(1))
                log.info("rejected IMEI %s (from %s)", c.group(1), self._client())
                return self._send(204)
            if method == "GET" and self.path == "/v1/devices":
                return self._send(200, {"devices": device_ids()})
            if method == "POST" and self.path == "/v1/devices":
                body = self._json_body()
                result = create(body.get("id") if isinstance(body, dict) else None)
                log.info("created %s (from %s)", result["id"], self._client())
                return self._send(201, result)
            if m and method == "GET" and not m.group(2):
                check_id(m.group(1))
                if m.group(1) not in broker_logins():
                    raise ApiError(404, f"device '{m.group(1)}' does not exist")
                return self._send(200, device_info(m.group(1)))
            if m and method == "POST" and m.group(2):
                result = rotate(m.group(1))
                log.info("rotated %s (from %s)", m.group(1), self._client())
                return self._send(200, result)
            if m and method == "DELETE" and not m.group(2):
                delete(m.group(1))
                log.info("deleted %s (from %s)", m.group(1), self._client())
                return self._send(204)
            raise ApiError(404, "not found")
        except ApiError as e:
            return self._send(e.status, {"error": e.message})
        except Exception:
            log.exception("%s %s failed", method, self.path)
            return self._send(500, {"error": "internal error, see the server log"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, fmt, *args):
        pass   # requests are logged above, without bodies


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_claims()
    start_mqtt()
    server = ThreadingHTTPServer(LISTEN, Handler)
    log.info("listening on %s:%d", *LISTEN)
    server.serve_forever()


if __name__ == "__main__":
    main()
