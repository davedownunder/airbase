#!/usr/bin/env python3
"""Airbase replacement app — local web UI + JSON API for Daikin Airbase (BRP15B61).

Zero dependencies: Python 3 stdlib only.

    python3 server.py                 # uses config.json / defaults
    AIRBASE=192.168.0.50 python3 server.py

The Airbase wifi module exposes an unauthenticated HTTP API under /skyfi/.
This server proxies it (avoiding CORS), parses the key=value responses into
JSON, and serves the web UI from ./static.

With "auth": true in config.json (or AUTH=1), the app requires an account:
the first visitor signs up freely, everyone after that needs the invite code
(shown on the signed-in UI and in the server log). Enable this before
exposing the app beyond your LAN — the aircon itself has no auth at all.
"""

import hashlib
import http.cookies
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {"airbase": "192.168.127.1", "port": 8585, "auth": False}

MODE_NAMES = {"0": "fan", "1": "heat", "2": "cool", "3": "auto", "7": "dry"}
MODE_CODES = {v: k for k, v in MODE_NAMES.items()}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    if os.environ.get("AIRBASE"):
        cfg["airbase"] = os.environ["AIRBASE"]
    if os.environ.get("PORT"):
        cfg["port"] = int(os.environ["PORT"])
    if os.environ.get("AUTH"):
        cfg["auth"] = os.environ["AUTH"] not in ("0", "false", "no")
    return cfg


CONFIG = load_config()


# ---------------------------------------------------------------- auth store

AUTH_PATH = os.path.join(BASE_DIR, "auth.json")
SESSION_TTL = 30 * 24 * 3600          # 30 days, sliding
PBKDF2_ITERATIONS = 200_000
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 900

_auth_lock = threading.Lock()
_failed_logins = {}                    # "ip|email" -> [count, lock_until]


def _load_auth():
    if os.path.exists(AUTH_PATH):
        with open(AUTH_PATH) as f:
            auth = json.load(f)
    else:
        auth = {}
    auth.setdefault("users", {})
    auth.setdefault("sessions", {})
    auth.setdefault("invite_code", secrets.token_urlsafe(9))
    return auth


AUTH = _load_auth()


def _save_auth():
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(AUTH, f, indent=2)
    os.replace(tmp, AUTH_PATH)


def _hash_password(password, salt_hex, iterations=PBKDF2_ITERATIONS):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations)
    return dk.hex()


def create_user(email, password, invite_code):
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("enter a valid email address")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with _auth_lock:
        if AUTH["users"]:
            if not secrets.compare_digest(invite_code or "", AUTH["invite_code"]):
                raise PermissionError("an invite code is required to sign up")
        if email in AUTH["users"]:
            raise ValueError("that email is already registered")
        salt = secrets.token_hex(16)
        AUTH["users"][email] = {
            "salt": salt,
            "hash": _hash_password(password, salt),
            "iterations": PBKDF2_ITERATIONS,
            "created": int(time.time()),
        }
        _save_auth()
    return email


def check_login(email, password, client_ip):
    email = email.strip().lower()
    key = f"{client_ip}|{email}"
    now = time.time()
    count, lock_until = _failed_logins.get(key, (0, 0))
    if now < lock_until:
        raise PermissionError(f"too many attempts — locked for {int(lock_until - now)}s")
    user = AUTH["users"].get(email)
    computed = _hash_password(password, user["salt"], user.get("iterations", PBKDF2_ITERATIONS)) if user else _hash_password(password, "00" * 16)
    if not user or not secrets.compare_digest(computed, user.get("hash", "")):
        count += 1
        _failed_logins[key] = (count, now + LOCKOUT_SECONDS if count >= MAX_FAILED_LOGINS else 0)
        raise PermissionError("wrong email or password")
    _failed_logins.pop(key, None)
    return email


def create_session(email):
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        AUTH["sessions"][token] = {"email": email, "exp": int(time.time()) + SESSION_TTL}
        # Opportunistically drop expired sessions.
        now = time.time()
        for t in [t for t, s in AUTH["sessions"].items() if s["exp"] < now]:
            del AUTH["sessions"][t]
        _save_auth()
    return token


def session_email(token):
    if not token:
        return None
    sess = AUTH["sessions"].get(token)
    now = int(time.time())
    if not sess or sess["exp"] < now:
        return None
    # Sliding expiry; persist at most once a day per session.
    if sess["exp"] < now + SESSION_TTL - 86400:
        with _auth_lock:
            sess["exp"] = now + SESSION_TTL
            _save_auth()
    return sess["email"]


def drop_session(token):
    with _auth_lock:
        if AUTH["sessions"].pop(token, None) is not None:
            _save_auth()


# ------------------------------------------------------------- airbase client

class AirbaseError(Exception):
    pass


def airbase_get(path, raw_query=""):
    """GET http://<airbase>/skyfi/<path> and parse ret=OK,k=v,... into a dict.

    Returns (parsed_dict, raw_pairs) where raw_pairs preserves the exact
    percent-encoded values (needed to echo zone_name back on writes).
    """
    url = f"http://{CONFIG['airbase']}/skyfi/{path}"
    if raw_query:
        url += "?" + raw_query
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            body = resp.read().decode("ascii", errors="replace").strip()
    except OSError as e:
        raise AirbaseError(f"cannot reach Airbase at {CONFIG['airbase']}: {e}") from e
    raw = {}
    parsed = {}
    for part in body.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        raw[k] = v
        parsed[k] = urllib.parse.unquote(v)
    if parsed.get("ret", "").upper() not in ("OK", "PARAM NG - ACT"):
        raise AirbaseError(f"Airbase returned {body[:120]!r} for {path}")
    return parsed, raw


_state_lock = threading.Lock()


_model_cache = None


def get_model():
    global _model_cache
    if _model_cache is None:
        try:
            _model_cache, _ = airbase_get("aircon/get_model_info")
        except AirbaseError:
            _model_cache = {}
    return _model_cache


def get_status():
    basic, _ = airbase_get("common/basic_info")
    ctrl, _ = airbase_get("aircon/get_control_info")
    model = get_model()
    try:
        sensor, _ = airbase_get("aircon/get_sensor_info")
    except AirbaseError:
        sensor = {}
    zones = []
    zone_raw = None
    if basic.get("en_zone") != "0" or basic.get("en_setzone") == "1":
        try:
            zparsed, zraw = airbase_get("aircon/get_zone_setting")
            zone_raw = zraw
            names = zparsed.get("zone_name", "").split(";")
            onoff = zparsed.get("zone_onoff", "").split(";")
            for i, name in enumerate(names):
                name = name.strip()
                if not name or name == "-":
                    continue
                zones.append({
                    "id": i,
                    "name": name,
                    "on": (onoff[i].strip() == "1") if i < len(onoff) else False,
                })
        except AirbaseError:
            pass

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    modes = ["cool", "heat"]
    if model.get("en_auto") == "1":
        modes.insert(0, "auto")
    if model.get("en_dry", "1") == "1":
        modes.append("dry")
    modes.append("fan")

    return {
        "name": basic.get("name", "Daikin"),
        "power": ctrl.get("pow") == "1",
        "mode": MODE_NAMES.get(ctrl.get("mode"), ctrl.get("mode")),
        "target_temp": num(ctrl.get("stemp")),
        "fan_rate": ctrl.get("f_rate"),
        "fan_auto": ctrl.get("f_auto") == "1",
        "inside_temp": num(sensor.get("htemp")),
        "outside_temp": num(sensor.get("otemp")),
        "zones": zones,
        "error_code": ctrl.get("err") or basic.get("err"),
        "capabilities": {
            "modes": modes,
            "fan_steps": int(model.get("frate_steps") or 3),
            "fan_auto": model.get("en_frate_auto") == "1",
            "temp_range": {
                "cool": [num(model.get("cool_l")) or 17, num(model.get("cool_h")) or 32],
                "heat": [num(model.get("heat_l")) or 16, num(model.get("heat_h")) or 31],
            },
        },
        "raw": {"basic": basic, "control": ctrl, "sensor": sensor, "model": model},
    }, zone_raw


def set_control(changes):
    """Merge changes into current control state and write it back.

    The Airbase requires the full parameter set on every write, so we read
    first, then send everything.
    """
    with _state_lock:
        ctrl, _ = airbase_get("aircon/get_control_info")
        pow_ = "1" if changes.get("power", ctrl.get("pow") == "1") else "0"
        mode = MODE_CODES.get(changes.get("mode"), ctrl.get("mode", "3"))
        stemp = changes.get("target_temp")
        if stemp is None:
            stemp = ctrl.get("stemp", "24")
        f_rate = changes.get("fan_rate") or ctrl.get("f_rate", "1")
        f_auto = ctrl.get("f_auto", "0")
        if "fan_auto" in changes:
            f_auto = "1" if changes["fan_auto"] else "0"
        params = {
            "pow": pow_,
            "mode": mode,
            "stemp": str(int(float(stemp))) if str(stemp) not in ("--", "") else ctrl.get("dt1", "24"),
            "f_rate": f_rate,
            "f_airside": ctrl.get("f_airside", "0"),
            "f_auto": f_auto,
            "f_dir": ctrl.get("f_dir", "0"),
            "lpw": "",
        }
        query = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
        airbase_get("aircon/set_control_info", query)
        time.sleep(1.2)  # the unit applies writes asynchronously (~1s)


def set_zone(zone_id, on):
    with _state_lock:
        zparsed, zraw = airbase_get("aircon/get_zone_setting")
        onoff = urllib.parse.unquote(zraw.get("zone_onoff", "")).split(";")
        if not (0 <= zone_id < len(onoff)):
            raise AirbaseError(f"zone {zone_id} out of range")
        onoff[zone_id] = "1" if on else "0"
        # Echo zone_name back exactly as the unit sent it.
        query = (
            f"zone_name={zraw.get('zone_name', '')}"
            f"&zone_onoff={urllib.parse.quote(';'.join(onoff), safe='')}"
        )
        airbase_get("aircon/set_zone_setting", query)
        time.sleep(1.2)  # the unit applies writes asynchronously (~1s)


# --------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    server_version = "AirbaseApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, code=200, set_cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    # ----- auth helpers

    def _client_ip(self):
        return (
            self.headers.get("CF-Connecting-IP")
            or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or self.client_address[0]
        )

    def _cookie_token(self):
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        morsel = jar.get("session")
        return morsel.value if morsel else None

    def _session_cookie(self, token, max_age=SESSION_TTL):
        cookie = f"session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        if self.headers.get("X-Forwarded-Proto") == "https":
            cookie += "; Secure"
        return cookie

    def _current_user(self):
        if not CONFIG["auth"]:
            return "local"
        return session_email(self._cookie_token())

    def _reject_unauthenticated(self, path):
        """Send a 401 (API) or login redirect (pages). Returns True if rejected."""
        if self._current_user():
            return False
        if path.startswith("/api/"):
            self._send_json({"error": "not signed in", "auth": True}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        return True

    # ----- routes

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/login":
                if CONFIG["auth"] and not self._current_user():
                    self._send_file("login.html", "text/html; charset=utf-8")
                else:
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.end_headers()
            elif path == "/api/auth/me":
                user = self._current_user()
                if user:
                    self._send_json({
                        "email": user,
                        "auth": CONFIG["auth"],
                        "invite_code": AUTH["invite_code"] if CONFIG["auth"] else None,
                    })
                else:
                    self._send_json({"error": "not signed in", "auth": True}, 401)
            elif self._reject_unauthenticated(path):
                return
            elif path == "/api/status":
                status, _ = get_status()
                self._send_json(status)
            elif path == "/api/config":
                self._send_json({"airbase": CONFIG["airbase"]})
            elif path in ("/", "/index.html"):
                self._send_file("index.html", "text/html; charset=utf-8")
            else:
                self.send_error(404)
        except AirbaseError as e:
            self._send_json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001 - report to client, keep serving
            self._send_json({"error": f"internal error: {e}"}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/auth/signup":
                email = create_user(body.get("email", ""), body.get("password", ""),
                                    body.get("invite_code", ""))
                token = create_session(email)
                self._send_json({"email": email}, set_cookie=self._session_cookie(token))
            elif path == "/api/auth/login":
                email = check_login(body.get("email", ""), body.get("password", ""),
                                    self._client_ip())
                token = create_session(email)
                self._send_json({"email": email}, set_cookie=self._session_cookie(token))
            elif path == "/api/auth/logout":
                drop_session(self._cookie_token())
                self._send_json({"ok": True}, set_cookie=self._session_cookie("", max_age=0))
            elif self._reject_unauthenticated(path):
                return
            elif path == "/api/control":
                set_control(body)
                status, _ = get_status()
                self._send_json(status)
            elif path == "/api/zone":
                set_zone(int(body["id"]), bool(body["on"]))
                status, _ = get_status()
                self._send_json(status)
            else:
                self.send_error(404)
        except AirbaseError as e:
            self._send_json({"error": str(e)}, 502)
        except PermissionError as e:
            self._send_json({"error": str(e)}, 403)
        except (KeyError, ValueError) as e:
            self._send_json({"error": f"bad request: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"internal error: {e}"}, 500)

    def _send_file(self, name, ctype):
        fpath = os.path.join(BASE_DIR, "static", name)
        if not os.path.exists(fpath):
            self.send_error(404)
            return
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    addr = ("0.0.0.0", CONFIG["port"])
    httpd = ThreadingHTTPServer(addr, Handler)
    print(f"Airbase app: http://localhost:{CONFIG['port']}  ->  controller {CONFIG['airbase']}")
    if CONFIG["auth"]:
        state = f"{len(AUTH['users'])} account(s)" if AUTH["users"] else "no accounts yet — first signup is open"
        print(f"Auth: ON ({state}). Invite code for additional signups: {AUTH['invite_code']}")
    else:
        print("Auth: off (LAN mode). Set \"auth\": true in config.json before exposing to the internet.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
