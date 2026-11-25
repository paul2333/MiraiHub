#!/usr/bin/env bash
# 幂等安装 WARP 出口（wireproxy）到 /opt/marvis-egress + systemd。
#
# 前置：/opt/marvis-egress/bin/{wireproxy,wgcf}
#   wireproxy v1.1.3  https://github.com/pufferffish/wireproxy/releases
#   wgcf（最新）      https://github.com/ViRb3/wgcf/releases
# 首次建出口：python3 mk_exit.py <socks_port> <http_port> w1
#             python3 mk_exit.py <socks_port> <http_port> w4
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exits="${MARVIS_EGRESS_EXITS:-w1 w4}"

install -d -m 0755 /opt/marvis-egress /opt/marvis-egress/bin \
  /opt/marvis-egress/logs /opt/marvis-egress/exits
install -m 0644 "$here/marvis-egress@.service" /etc/systemd/system/
install -m 0644 "$here/marvis-egress-refresh.service" /etc/systemd/system/
install -m 0644 "$here/marvis-egress-refresh.timer" /etc/systemd/system/
install -m 0755 "$here/refresh_exits.sh" /opt/marvis-egress/
install -m 0644 "$here/mk_exit.py" /opt/marvis-egress/

for bin in wireproxy wgcf; do
  if [ ! -x "/opt/marvis-egress/bin/$bin" ]; then
    echo "缺少 /opt/marvis-egress/bin/$bin —— 先放二进制再装" >&2
    exit 2
  fi
done

systemctl daemon-reload
for exit in $exits; do
  systemctl enable --now "marvis-egress@$exit"
done
systemctl enable --now marvis-egress-refresh.timer

echo "--- 自检 ---"
for exit in $exits; do
  port=$(grep -oE 'BindAddress = 0\.0\.0\.0:[0-9]+' "/opt/marvis-egress/exits/$exit/wireproxy.conf" 2>/dev/null | cut -d: -f2 || true)
  if [ -n "${port:-}" ]; then
    echo "$exit -> $(curl -sS -m 10 -x "http://127.0.0.1:$port" https://api.ipify.org || echo FAIL)"
  else
    echo "$exit -> 未生成 wireproxy.conf"
  fi
done
echo "compose 侧：MARVIS_EGRESS_POOL=http://host.docker.internal:42001,http://host.docker.internal:42004"
