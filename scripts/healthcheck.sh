#!/bin/bash
# workbuddy2api 健康自愈（systemd 版）
#
# 为什么不是 scripts/watchdog.sh：
#   那个脚本是给「宝塔面板 cron」写的，会 runuser 启一个 main.py。线上现在是
#   systemd 管 workbuddy2api.service（Restart=always），两者同时拉起会抢端口、
#   留双实例。这里只做一件事：连续不健康就 systemctl restart，把进程管理交回
#   systemd。
#
# 判定口径（2026-11 起）：
#   * 用独立 liveness（GET /api/app/version）：排空期间它仍是 200，
#     所以"主动排空"不会累计故障，也不会被当成故障去重启。
#   * 503 且响应体带 service_draining -> 主动排空，明确不累计故障。
#   * 404 -> 旧版没有状态接口，不据此重启（避免把旧版当成故障）。
#   * 连接超时/拒绝（000）按"瞬时不可达"处理：连续 3 次（约 3 分钟）才重启，
#     单次超时绝不当成"可以安全强杀"。
#
# 安装（root）：
#   install -m 755 scripts/healthcheck.sh /usr/local/bin/workbuddy2api-healthcheck.sh
#   systemd timer 或 cron 每分钟调用一次即可。

STATE=${WB_HEALTH_STATE:-/var/tmp/wb2api-health.fail}
PORT=${WB_PORT:-8790}
URL_LIVE="http://127.0.0.1:${PORT}/api/app/version"
URL_READY="http://127.0.0.1:${PORT}/api/app/ready"
SERVICE=${WB_SERVICE:-workbuddy2api.service}
THRESHOLD=${WB_HEALTH_THRESHOLD:-3}

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$URL_LIVE" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  # 存活正常：不论排空与否都算健康，清零计数。
  [ -f "$STATE" ] && rm -f "$STATE"
  exit 0
fi

if [ "$code" = "404" ]; then
  logger -t wb2api-health "旧版没有 /api/app/version（HTTP 404）：不据此重启"
  exit 0
fi

body=$(curl -s --max-time 8 "$URL_READY" 2>/dev/null || true)
case "$body" in
  *service_draining*|*'"ready": false'*|*'"ready":false'*)
    logger -t wb2api-health "服务正在主动排空（liveness HTTP $code）：不累计故障"
    exit 0
    ;;
esac

n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
logger -t wb2api-health "健康检查失败 code=$code 连续=$n/$THRESHOLD"
if [ "$n" -ge "$THRESHOLD" ]; then
  logger -t wb2api-health "连续 $n 次不健康，重启 $SERVICE（属外部硬截止，不用于正常排空）"
  systemctl restart "$SERVICE"
  rm -f "$STATE"
fi
exit 0
