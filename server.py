#!/usr/bin/env python3
"""Airbase replacement app — local web UI + JSON API for Daikin Airbase (BRP15B61).

Zero dependencies: Python 3 stdlib only.

    python3 server.py                 # uses config.json / defaults
    AIRBASE=192.168.0.50 python3 server.py

The Airbase wifi module exposes an unauthenticated HTTP API under /skyfi/.
This server proxies it (avoiding CORS), parses the key=value responses into
JSON, and serves the web UI from ./static.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {"airbase": "192.168.127.1", "port": 8585}

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
    return cfg


CONFIG = load_config()


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


class Handler(BaseHTTPRequestHandler):
    server_version = "AirbaseApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/status":
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
            if path == "/api/control":
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
    httpd.serve_forever()


if __name__ == "__main__":
    main()
