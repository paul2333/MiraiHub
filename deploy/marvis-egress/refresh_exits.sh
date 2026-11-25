#!/bin/bash
# 每天刷新 WARP 出口 IP：重新注册 WARP 账号（新私钥）→ 重建 profile → 重启 wireproxy。
#
# 为什么不用「换 endpoint」：换端点会把两个出口都赶到同一个 colo，IP 反而撞一起。
# 重新注册必然换 IP，且各出口保持各自的端点族（w1=188.114.x / w4=162.159.x）。
#
# 出口集合由 MARVIS_EGRESS_EXITS 决定（默认 "w1 w4"）；每个出口的端点与本地
# HTTP 端口见 endpoint_for / port_for。新增出口时三处同步：本脚本、compose 的
# MARVIS_EGRESS_POOL、install.sh 的 enable 列表。
set -u
LOG=/opt/marvis-egress/logs/refresh.log
BIN=/opt/marvis-egress/bin
EXITS=${MARVIS_EGRESS_EXITS:-"w1 w4"}

endpoint_for() {
  case "$1" in
    w1) echo 188.114.98.224 ;;
    w4) echo 162.159.192.1 ;;
    *) echo "" ;;
  esac
}

port_for() {
  case "$1" in
    w1) echo 42001 ;;
    w4) echo 42004 ;;
    *) echo "" ;;
  esac
}

echo "=== $(date -Is) 开始刷新 ===" | tee -a "$LOG"
for exit in $EXITS; do
  d=/opt/marvis-egress/exits/$exit
  ep=$(endpoint_for "$exit")
  port=$(port_for "$exit")
  if [ ! -d "$d" ] || [ -z "$ep" ] || [ -z "$port" ]; then
    echo "  $exit 未配置（目录/端点/端口缺一），跳过" | tee -a "$LOG"
    continue
  fi
  old_ip=$(curl -sS -m 12 -x "http://127.0.0.1:$port" https://api.ipify.org 2>/dev/null)
  cd "$d" || continue
  rm -f wgcf-account.toml wgcf-profile.conf
  "$BIN/wgcf" register --accept-tos >/dev/null 2>&1
  "$BIN/wgcf" generate >/dev/null 2>&1
  if [ ! -f wgcf-profile.conf ]; then
    echo "  $exit 注册失败" | tee -a "$LOG"
    continue
  fi
  python3 - "$d" "$ep" "$port" <<'PYEOF'
import re, sys
d, ep, port = sys.argv[1], sys.argv[2], sys.argv[3]
conf = open(d + '/wgcf-profile.conf').read()

def grab(sec, key):
    m = re.search(r'^\[' + sec + r'\]$(.*?)(?=^\[|\Z)', conf, re.S | re.M)
    if not m:
        return None
    mm = re.search(r'^' + key + r'\s*=\s*(.+)$', m.group(1), re.M)
    return mm.group(1).strip() if mm else None

wp = ('[Interface]\nAddress = %s\nPrivateKey = %s\nDNS = %s\n\n'
      '[Peer]\nPublicKey = %s\nEndpoint = %s:2408\nAllowedIPs = 0.0.0.0/0, ::/0\n\n'
      '[HTTP]\nBindAddress = 0.0.0.0:%s\n') % (
          grab('Interface', 'Address'), grab('Interface', 'PrivateKey'),
          grab('Interface', 'DNS') or '1.1.1.1', grab('Peer', 'PublicKey'),
          ep, port)
open(d + '/wireproxy.conf', 'w').write(wp)
print('  rebuilt', d)
PYEOF
  systemctl restart "marvis-egress@$exit"
  sleep 9
  new_ip=$(curl -sS -m 20 -x "http://127.0.0.1:$port" https://api.ipify.org 2>/dev/null)
  echo "  $exit  ip: $old_ip -> $new_ip" | tee -a "$LOG"
done
echo "=== $(date -Is) 刷新结束 ===" | tee -a "$LOG"
