#!/usr/bin/env bash
# sidecar_stop.sh -- 停掉签名侧车（整个进程组，含 wine 的子进程）
set -uo pipefail
PIDFILE=${SIDECAR_PIDFILE:-/tmp/marvis-sidecar.pid}
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
pkill -f 'node.exe sidecar.js' 2>/dev/null || true
sleep 0.3
if pgrep -f 'node.exe sidecar.js' >/dev/null 2>&1; then echo "still-running"; exit 1; fi
echo "stopped"
