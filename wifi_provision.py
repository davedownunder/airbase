#!/usr/bin/env python3
"""Provision a Daikin Airbase (BRP15B61) onto your home WiFi — without the app.

Runs on macOS. Temporarily joins the module's own DaikinAP network, talks to
its HTTP API at 192.168.127.1, then restores this Mac's WiFi. Internet drops
for ~30-60s while it runs; the script is fully self-contained and always
attempts to restore WiFi, even on failure.

Step 1 — read-only discovery (finds which wifi-config endpoints the firmware has):

    python3 wifi_provision.py discover \
        --ap-ssid "DaikinAP12345" --ap-key "0123456789" \
        --home-ssid "YourHomeWifi"

Step 2 — actually configure it (uses what discovery found):

    python3 wifi_provision.py provision \
        --ap-ssid "DaikinAP12345" --ap-key "0123456789" \
        --home-ssid "YourHomeWifi" \
        --ssid "YourIoTNetwork" --password "wifi-password"

Everything is logged to wifi_provision.log (JSON lines).
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IFACE = "en0"
AIRBASE = "192.168.127.1"
LOG_PATH = "wifi_provision.log"

READ_PROBES = [
    "skyfi/common/basic_info",
    "common/basic_info",
    "skyfi/common/get_wifi_setting",
    "common/get_wifi_setting",
    "skyfi/common/get_ap",
    "common/get_ap",
    "skyfi/common/get_remote_method",
    "common/get_remote_method",
]

log_entries = []


def log(event, **data):
    entry = {"t": time.strftime("%H:%M:%S"), "event": event, **data}
    log_entries.append(entry)
    print(f"[{entry['t']}] {event}: {json.dumps(data)[:300]}", flush=True)


def flush_log():
    with open(LOG_PATH, "a") as f:
        for e in log_entries:
            f.write(json.dumps(e) + "\n")


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def wifi_ip():
    r = sh("ipconfig", "getifaddr", IFACE)
    return r.stdout.strip() or None


def join_network(ssid, key=None):
    cmd = ["networksetup", "-setairportnetwork", IFACE, ssid]
    if key:
        cmd.append(key)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    log("join_attempt", ssid=ssid, out=(r.stdout + r.stderr).strip())
    return r


def wait_for_ip(predicate, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = wifi_ip()
        if ip and predicate(ip):
            return ip
        time.sleep(2)
    return None


def http_get(path_and_query, timeout=6):
    url = f"http://{AIRBASE}/{path_and_query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(2000).decode("ascii", errors="replace").strip()
            log("http", path=path_and_query, status=resp.status, body=body)
            return body
    except Exception as e:  # noqa: BLE001 - offline tool, log and continue
        log("http_error", path=path_and_query, error=str(e))
        return None


def restore_wifi(home_ssid, home_password=None):
    log("restore_start", ssid=home_ssid)
    ip = None
    if home_ssid:
        join_network(home_ssid, home_password)
        ip = wait_for_ip(lambda ip: not ip.startswith("192.168.127."), timeout=40)
    if not ip:
        # Fall back to power-cycling WiFi so macOS auto-joins a preferred network.
        log("restore_powercycle")
        sh("networksetup", "-setairportpower", IFACE, "off")
        time.sleep(2)
        sh("networksetup", "-setairportpower", IFACE, "on")
        ip = wait_for_ip(lambda ip: not ip.startswith("192.168.127."), timeout=60)
    log("restore_done", ip=ip)
    return ip


def connect_to_daikin(ap_ssid, ap_key):
    # networksetup fails with "Could not find network" when the SSID isn't in
    # its scan cache yet — power-cycling WiFi and retrying usually fixes it.
    ip = None
    for attempt in range(4):
        if attempt:
            log("daikin_join_retry", attempt=attempt)
            sh("networksetup", "-setairportpower", IFACE, "off")
            time.sleep(2)
            sh("networksetup", "-setairportpower", IFACE, "on")
            time.sleep(8)
        r = join_network(ap_ssid, ap_key)
        if "Could not find network" in (r.stdout + r.stderr):
            continue
        ip = wait_for_ip(lambda ip: ip.startswith("192.168.127."), timeout=45)
        if ip:
            break
    if not ip:
        log("daikin_join_failed")
        return False
    log("daikin_joined", my_ip=ip)
    return http_get("skyfi/common/basic_info") is not None or http_get("common/basic_info") is not None


def do_discover(args):
    ok = connect_to_daikin(args.ap_ssid, args.ap_key)
    if ok:
        for p in READ_PROBES:
            http_get(p)
    restore_wifi(args.home_ssid, args.home_password)
    return 0 if ok else 1


def hexenc(s):
    return s.encode().hex()


def do_provision(args):
    if not connect_to_daikin(args.ap_ssid, args.ap_key):
        restore_wifi(args.home_ssid, args.home_password)
        return 1

    # Find which prefix this firmware answers wifi-setting reads on.
    prefix = None
    for pfx in ("skyfi/common", "common"):
        body = http_get(f"{pfx}/get_wifi_setting")
        if body and "ret=OK" in body:
            prefix = pfx
            break
    if prefix is None:
        log("abort", reason="no get_wifi_setting endpoint answered ret=OK; not writing anything")
        restore_wifi(args.home_ssid, args.home_password)
        return 2

    ssid_q = urllib.parse.quote(args.ssid, safe="")
    stored = False
    # The Daikin adapter family expects the passphrase hex-encoded; fall back
    # to plain if the firmware rejects that. Verify by reading the setting
    # back before committing to a reboot.
    attempts = [
        ("hex", f"{prefix}/set_wifi_setting?ssid={ssid_q}&security={args.security}&key={hexenc(args.password)}"),
        ("plain", f"{prefix}/set_wifi_setting?ssid={ssid_q}&security={args.security}&key={urllib.parse.quote(args.password, safe='')}"),
    ]
    for enc_name, query in attempts:
        body = http_get(query)
        if not (body and "ret=OK" in body):
            continue
        readback = http_get(f"{prefix}/get_wifi_setting") or ""
        if f"ssid={args.ssid}" in urllib.parse.unquote(readback):
            stored = True
            log("wifi_setting_stored", encoding=enc_name)
            break
        log("readback_mismatch", encoding=enc_name, readback=readback)
    if stored:
        # Reboot so the module leaves AP mode and joins the WLAN.
        http_get(f"{prefix}/reboot")
        log("rebooted", note="module should join the target network within ~1 minute")
    else:
        log("abort", reason="setting never verified via readback; module left in AP mode")
    wrote = stored

    restore_wifi(args.home_ssid, args.home_password)
    return 0 if wrote else 3


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["discover", "provision"])
    ap.add_argument("--ap-ssid", required=True, help="DaikinAP SSID (on the module sticker)")
    ap.add_argument("--ap-key", default=None, help="DaikinAP KEY (on the module sticker)")
    ap.add_argument("--home-ssid", default=None,
                    help="SSID this Mac should rejoin afterwards (omit to auto-rejoin via WiFi power-cycle)")
    ap.add_argument("--home-password", default=None, help="only needed if not saved in keychain")
    ap.add_argument("--ssid", help="target network for the aircon (2.4GHz, WPA2)")
    ap.add_argument("--password", help="target network password")
    ap.add_argument("--security", default="WPA2-PSK")
    args = ap.parse_args()

    if args.mode == "provision" and not (args.ssid and args.password):
        ap.error("provision mode requires --ssid and --password")

    try:
        rc = do_discover(args) if args.mode == "discover" else do_provision(args)
    finally:
        flush_log()
    sys.exit(rc)


if __name__ == "__main__":
    main()
