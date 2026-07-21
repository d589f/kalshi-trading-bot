#!/bin/bash
# Restart xo-paper if the unit is down OR the heartbeat is stale (hang detection).
# systemd Restart=always only catches crashes; this catches a wedged process.
# root cron: * * * * * /home/dmitrii/xo_paper_watchdog.sh >/dev/null 2>&1
HB=${XO_PAPER_HEARTBEAT:-/home/dmitrii/xo_paper/heartbeat}
now=$(date +%s); stale=1
if [ -f "$HB" ]; then
  hb=$(cut -d. -f1 "$HB" 2>/dev/null); case "$hb" in ''|*[!0-9]*) hb=0;; esac
  [ $((now - hb)) -le 120 ] && stale=0
fi
active=$(systemctl is-active xo-paper 2>/dev/null)
if [ "$active" != "active" ] || [ "$stale" = "1" ]; then
  logger -t xo-paper-wd "restart active=$active stale=$stale hb_age=$((now-${hb:-0}))"
  systemctl restart xo-paper
fi
