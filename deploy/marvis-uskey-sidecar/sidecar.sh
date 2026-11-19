#!/usr/bin/env bash
# sidecar.sh -- 启动 Marvis uskey 签名侧车（Wine + Windows node.exe）
#
#   SIDECAR_HOST=127.0.0.1 SIDECAR_PORT=8791 ./sidecar.sh
#   SIDECAR_HOST=172.17.0.1 ./sidecar.sh     # 容器内经 docker0 网关访问
#
# 铁律：wine node.exe 的 stdout 若是管道会 "Error: open EBADF" 秒崩，
#       所以这里一律 >>"$LOG" 2>&1，绝不用管道。
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${WINEPREFIX:-}" ]; then export WINEPREFIX=/opt/marvis-wine/prefix; fi
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="
unset DISPLAY 2>/dev/null || true

if [ -z "${SIDECAR_HOST:-}" ]; then SIDECAR_HOST=127.0.0.1; fi
if [ -z "${SIDECAR_PORT:-}" ]; then SIDECAR_PORT=8791; fi
export SIDECAR_HOST SIDECAR_PORT
LOG=${SIDECAR_LOG:-/tmp/marvis-sidecar.log}
PIDFILE=${SIDECAR_PIDFILE:-/tmp/marvis-sidecar.pid}

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already-running pid=$(cat "$PIDFILE") host=$SIDECAR_HOST:$SIDECAR_PORT"
  exit 0
fi

: >"$LOG"
setsid bash -c 'cd /opt/marvis-wine && exec wine node.exe sidecar.js' >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 150); do
  if curl -fsS --max-time 2 "http://$SIDECAR_HOST:$SIDECAR_PORT/health" >/tmp/.sidecar-health 2>/dev/null; then
    echo "ready pid=$(cat "$PIDFILE") host=$SIDECAR_HOST:$SIDECAR_PORT"
    cat /tmp/.sidecar-health; echo; rm -f /tmp/.sidecar-health
    exit 0
  fi
  sleep 0.2
done
echo "TIMEOUT (see $LOG)" >&2
tail -30 "$LOG" >&2 || true
exit 1
