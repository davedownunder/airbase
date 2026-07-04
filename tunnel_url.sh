#!/bin/sh
# Print the app's current public URL from the cloudflared quick-tunnel log.
# (Quick-tunnel URLs change whenever the tunnel restarts.)
LOG="${1:-$HOME/Library/Logs/airbase/tunnel.log}"
grep -o "https://[a-z-]*\.trycloudflare\.com" "$LOG" | tail -1
