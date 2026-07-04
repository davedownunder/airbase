# Airbase

A local, no-cloud replacement for the Daikin Airbase app. Control a ducted
Daikin air conditioner with a **BRP15B61** wifi module from any browser on
your network — power, mode, temperature, fan speed, and zones.

**[▶ Live demo](https://davedownunder.github.io/airbase/)** (simulated aircon, runs entirely in your browser)

- **Zero dependencies** — one Python 3 file, stdlib only. Runs on a Mac, a
  Raspberry Pi, your Home Assistant box, anything.
- **No cloud, no account, no official app** — talks straight to the module's
  built-in HTTP API on your LAN.
- **Capability-aware UI** — modes, fan steps, temperature ranges, and zones
  adapt to what your unit reports via `get_model_info`.
- **WiFi onboarding without the official app** — `wifi_provision.py` moves
  the module from its own `DaikinAP...` access point onto your home network
  over its HTTP API (macOS).

## Quick start

```bash
git clone https://github.com/davedownunder/airbase.git
cd airbase
cp config.example.json config.json   # put your Airbase's IP in here
python3 server.py
```

Open `http://<host>:8585` from any phone or laptop on your network.
`AIRBASE=<ip[:port]>` and `PORT=<port>` environment variables override the config.

Don't know the module's IP? If it's already on your network:

```bash
curl "http://<candidate-ip>/skyfi/common/basic_info"   # ret=OK,type=aircon,... = found it
```

## Getting the module onto your WiFi (no official app needed)

Out of the box the BRP15B61 broadcasts its own access point (SSID
`DaikinAP#####`, key on the module's sticker) at `192.168.127.1` — which is
why the official app makes you switch WiFi networks. Move it onto your LAN
once and never think about it again.

On macOS, `wifi_provision.py` does it over the module's HTTP API. It
temporarily joins the DaikinAP (your internet drops for ~1 minute), verifies
the setting by reading it back before rebooting the module, and always
restores your Mac's WiFi:

```bash
# 1. Read-only discovery — confirms the module and its endpoints, writes nothing
python3 wifi_provision.py discover --ap-ssid "DaikinAP12345" --ap-key "<KEY on sticker>"

# 2. Provision — tells the module to join your network, then reboots it
python3 wifi_provision.py provision --ap-ssid "DaikinAP12345" --ap-key "<KEY>" \
    --ssid "<YourWifi>" --password "<wifi password>"
```

Notes:

- The module is **2.4 GHz-only and WPA2** — a WPA3-only or 5 GHz-only SSID
  won't work. On UniFi, a 2.4 GHz IoT SSID is ideal.
- After it joins, find its IP in your router's client list and give it a
  **fixed IP** so nothing ever loses it.
- Everything is logged to `wifi_provision.log`.

## Accounts & internet access

The aircon's own API is completely unauthenticated, so never expose it (or
this app in LAN mode) to the internet directly. The app has a built-in
account layer for exactly this — enable it with `"auth": true` in
`config.json` (or `AUTH=1`):

- The **first visitor signs up freely** and owns the unit.
- Every later signup needs the **invite code**, shown on the signed-in
  screen and in the server log — share it with your household.
- Passwords are stored PBKDF2-hashed in `auth.json` (gitignored); sessions
  are 30-day HttpOnly cookies; failed logins are rate-limited with a
  lockout.

For the internet-facing transport, use an encrypted tunnel instead of a port
forward — e.g. [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
cloudflared tunnel --url http://localhost:8585        # quick tunnel, random URL
# or a named tunnel + your own domain for a stable https://aircon.example.com
```

The app honours `X-Forwarded-Proto` from the tunnel, so session cookies are
marked `Secure` automatically when served over HTTPS.

## Home Assistant

Home Assistant supports the Airbase natively — no custom component:

1. Settings → Devices & Services → **Add Integration** → search **Daikin**.
2. Enter the module's IP. Leave API key/password blank (the Airbase has no auth).
3. You get a `climate` entity (power/mode/temp/fan) plus one `switch` per
   zone, ready for dashboards, automations, and voice assistants.

If HA is on a different VLAN than the aircon, allow traffic from HA to the
module on TCP 80 (and UDP 30050 for discovery). This app and HA coexist
fine — both just poll the unit's HTTP API.

## Development

`mock_airbase.py` simulates a BRP15B61 so you can hack on the app without
touching a real unit:

```bash
python3 mock_airbase.py                    # mock unit on 127.0.0.1:8125
AIRBASE=127.0.0.1:8125 python3 server.py   # app pointed at the mock
```

The browser demo (`?demo=1`, also what GitHub Pages serves) simulates the
aircon client-side — no backend at all. `docs/index.html` is a straight copy
of `static/index.html`; after UI changes, refresh it with:

```bash
cp static/index.html docs/index.html
```

## API notes (reverse-engineered Airbase HTTP API)

All endpoints are unauthenticated HTTP GETs under `/skyfi/`, returning
`ret=OK,key=value,...` text:

| Endpoint | Purpose |
|---|---|
| `common/basic_info` | name, power, firmware, zone support |
| `aircon/get_model_info` | capabilities: modes, fan steps, temp ranges |
| `aircon/get_control_info` | power, mode, set temp, fan rate |
| `aircon/set_control_info?pow=&mode=&stemp=&f_rate=...` | write settings (send the full set) |
| `aircon/get_sensor_info` | inside (`htemp`) / outside (`otemp`) temps |
| `aircon/get_zone_setting` | zone names + on/off (`;`-separated, URL-encoded) |
| `aircon/set_zone_setting?zone_name=&zone_onoff=` | write zones (echo names back) |
| `common/get_wifi_setting` / `set_wifi_setting?ssid=&security=&key=` | WLAN config (AP mode) |
| `common/reboot` | reboot the module |

Mode codes: `0` fan, `1` heat, `2` cool, `3` auto, `7` dry.
Fan rates: `1`–`5` = speed steps; `f_auto=1` = fan auto. Writes apply
asynchronously (~1s).

The app's own JSON API (used by the UI, handy for scripts):

- `GET /api/status` — full parsed state, including `capabilities`
- `POST /api/control` — `{"power": true, "mode": "cool", "target_temp": 24, "fan_rate": "3", "fan_auto": false}` (any subset)
- `POST /api/zone` — `{"id": 2, "on": true}`

## License

[MIT](LICENSE)
