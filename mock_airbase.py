#!/usr/bin/env python3
"""Simulates a Daikin Airbase (BRP15B61) wifi module for local development.

    python3 mock_airbase.py            # listens on 127.0.0.1:8125
    AIRBASE=127.0.0.1:8125 python3 server.py

Implements the /skyfi/ endpoints the app uses, with realistic responses.
"""

import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "pow": "1",
    "mode": "2",          # cool
    "stemp": "24",
    "f_rate": "1",
    "f_airside": "0",
    "f_auto": "0",
    "f_dir": "0",
    "zone_names": ["Living", "Kitchen", "Master Bed", "Kids", "-", "-", "-", "-"],
    "zone_onoff": ["1", "1", "0", "0", "0", "0", "0", "0"],
}


def enc(s):
    return urllib.parse.quote(s, safe="")


class Mock(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _reply(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path

        if path == "/skyfi/common/basic_info":
            self._reply(
                "ret=OK,type=aircon,reg=au,dst=1,ver=1_14_38,pow={pow},err=0,"
                "location=0,name={name},icon=0,method=home only,port=30050,id=,pw=,"
                "lpw_flag=0,adp_kind=0,led=1,en_setzone=1,mac=F8F005AABBCC,"
                "adp_mode=,ssid=MockNet,err_type=0,err_code=0,en_ch=1,holiday=0,"
                "en_hol=0,sync_time=0".format(pow=STATE["pow"], name=enc("Mock Aircon"))
            )
        elif path == "/skyfi/aircon/get_control_info":
            self._reply(
                "ret=OK,pow={pow},mode={mode},operate=1,bk_auto=2,stemp={stemp},"
                "dt1=25,dt2=21,f_rate={f_rate},dfr1=1,dfr2=1,f_airside={f_airside},"
                "airside1=0,airside2=0,f_auto={f_auto},auto1=0,auto2=0,"
                "f_dir={f_dir},filter_sign_info=0,cent=0,en_cent=0,remo=2".format(**STATE)
            )
        elif path == "/skyfi/aircon/set_control_info":
            for k in ("pow", "mode", "stemp", "f_rate", "f_airside", "f_auto", "f_dir"):
                if k in q:
                    STATE[k] = q[k]
            self._reply("ret=OK,adv=")
        elif path == "/skyfi/aircon/get_sensor_info":
            self._reply("ret=OK,err=0,htemp=23,otemp=31")
        elif path == "/skyfi/aircon/get_model_info":
            self._reply(
                "ret=OK,err=0,model=NOTSUPPORT,type=N,humd=0,s_humd=0,en_zone=8,"
                "en_auto=1,en_dry=1,frate_steps=3,en_frate_auto=1,"
                "cool_l=17,cool_h=32,heat_l=16,heat_h=31"
            )
        elif path == "/skyfi/aircon/get_zone_setting":
            self._reply(
                "ret=OK,zone_name={names},zone_onoff={onoff}".format(
                    names=enc(";".join(STATE["zone_names"])),
                    onoff=enc(";".join(STATE["zone_onoff"])),
                )
            )
        elif path == "/skyfi/aircon/set_zone_setting":
            if "zone_onoff" in q:
                STATE["zone_onoff"] = q["zone_onoff"].split(";")
            if "zone_name" in q:
                STATE["zone_names"] = q["zone_name"].split(";")
            self._reply("ret=OK")
        else:
            self._reply("Not Found")


if __name__ == "__main__":
    print("Mock Airbase on http://127.0.0.1:8125")
    ThreadingHTTPServer(("127.0.0.1", 8125), Mock).serve_forever()
