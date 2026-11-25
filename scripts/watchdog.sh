#!/bin/bash
# ===========================================================================
# workbuddy2api 看门狗（Linux / 宝塔面板）
# ---------------------------------------------------------------------------
# 作用：服务真的不在了才拉起；正在主动切换（starting/draining/stopping）时
#       什么都不做。
#
# 为什么需要它（真实事故，不是假想）：
#   2026-09-29 03:36 系统 dnf-makecache 刷元数据触发全机 OOM，内核杀掉了
#   uvicorn；看门狗确实被 cron 每 2 分钟触发，却连续 113 次全部失败在最后一步
#   （line 86: runuser: command not found）—— 根因是脚本没声明 PATH，而 cron
#   给的 PATH 极简（/usr/local/bin:/usr/bin），不含 /usr/sbin。
#   结果停机 3.5 小时。本脚本已修掉该问题（显式 PATH + runuser 绝对路径）。
#
# 2026-11 起判定口径变了（这一版最要紧的改动）：
#   * 一次 GET /admin 失败**不再**直接 kill 持有者。旧实现 kill → sleep → kill -9，
#     会把正在跑长请求、正在排空、正在关库的实例连人带连接一起砍断。
#   * 先问 main.py 自己的所有者状态（python main.py --status --json）：
#       starting / draining / stopping -> 主动切换，跳过破坏性恢复
#       unreachable                    -> 问不到 ≠ 可以安全强杀，本次不处置
#       failed                         -> 保留现场，交给人
#       none                           -> 确认没有所有者，直接拉起
#       ready 但页面不可用             -> 先请求它正常排空（--stop），
#                                         只有确认退出才拉起；不退出就只报告
#   * 单次失败不处置：连续 WB_FAIL_THRESHOLD（默认 2）次才进入恢复流程。
#   * 保留 OOM 保护与 flock 防重叠。
#
# 安装（root）：
#   install -m 755 scripts/watchdog.sh \
#       /www/server/python_project/vhost/scripts/workbuddy2api_watchdog.sh
#   crontab -e      # 追加一行：
#   */2 * * * * /www/server/python_project/vhost/scripts/workbuddy2api_watchdog.sh >/dev/null 2>&1
#
# 所有路径都可用环境变量覆盖（见下方 WB_* 默认值）。
# ===========================================================================
set -u

# ---------------------------------------------------------------------------
# 显式声明 PATH —— 本脚本最要紧的一行（原因见文件头）。
# 无论从 cron、systemd 还是交互式 shell 调用，都能找到 runuser / curl / ss。
# ---------------------------------------------------------------------------
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ------------------------------ 可配置项 -----------------------------------
PROJ=${WB_PROJ:-/www/wwwroot/workbuddy2api}
PY=${WB_PY:-/www/server/pyporject_evn/versions/3.10.20/bin/python}
RUN_USER=${WB_RUN_USER:-www}          # 服务以谁的身份跑（不要用 root）
PORT=${WB_PORT:-8790}
LOG=${WB_LOG:-/www/wwwlogs/python/workbuddy2api/watchdog.log}
PIDFILE=${WB_PIDFILE:-/www/server/python_project/vhost/pids/workbuddy2api.pid}
LOCK=${WB_LOCK:-/tmp/workbuddy2api-watchdog.lock}
ERRLOG=${WB_ERRLOG:-/www/wwwlogs/python/workbuddy2api/error.log}
OOM_ADJ=${WB_OOM_ADJ:--500}           # 负值 = OOM 时最后才被杀（需 root）
FAILSTATE=${WB_FAILSTATE:-/var/tmp/workbuddy2api-watchdog.fail}
THRESHOLD=${WB_FAIL_THRESHOLD:-2}     # 连续不健康多少次才进入恢复流程
STOP_WAIT=${WB_STOP_WAIT:-60}          # 请求正常排空的等待上限（秒）

# runuser 解析成绝对路径，避免再受 PATH 影响（双保险）。
RUNUSER=$(command -v runuser 2>/dev/null || true)
[ -n "${RUNUSER:-}" ] || RUNUSER=/usr/sbin/runuser

# Python 解释器兜底：默认路径不存在时自动找一个，别让脚本白跑。
if [ ! -x "$PY" ]; then
  for c in "$PROJ/.venv/bin/python3" /www/server/pyporject_evn/versions/*/bin/python \
           "$(command -v python3 2>/dev/null)"; do
    [ -x "${c:-}" ] && PY="$c" && break
  done
fi

mkdir -p "$(dirname "$LOG")" "$(dirname "$PIDFILE")" 2>/dev/null
touch "$LOG" 2>/dev/null

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 防止上一轮还没跑完就又起一轮（cron 重叠）
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

# ------------------------ 所有者状态（唯一可信来源） ------------------------
# 输出 ready / starting / draining / stopping / failed / none / unreachable。
# 端口上有没有人监听**不参与**判断：那不能证明归属。
owner_phase() {
  local out
  out=$("$PY" "$PROJ/main.py" --status --json --port "$PORT" 2>/dev/null) || true
  if [ -z "${out:-}" ]; then
    echo "unreachable"
    return 0
  fi
  printf '%s' "$out" | "$PY" -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("unreachable")
    raise SystemExit(0)
print(data.get("phase") or "unreachable")' 2>/dev/null || echo unreachable
}

# ------------------------------ 拉起 ---------------------------------------
start_service() {
  cd "$PROJ" || { log "拉起失败：项目目录不存在 $PROJ"; return 1; }
  chown -R "$RUN_USER:$RUN_USER" "$PROJ/logs" "$(dirname "$LOG")" 2>/dev/null
  chown "$RUN_USER:$RUN_USER" "$PROJ/.env" 2>/dev/null

  "$RUNUSER" -u "$RUN_USER" -- bash -c \
    "cd '$PROJ' && nohup '$PY' main.py --port $PORT >> '$ERRLOG' 2>&1 & echo \$!" \
    > /tmp/wb_wd_pid 2>/tmp/wb_wd_err

  NEW=$(tr -dc '0-9' < /tmp/wb_wd_pid 2>/dev/null)
  if [ -z "${NEW:-}" ]; then
    log "拉起失败：$(cat /tmp/wb_wd_err 2>/dev/null | tail -3)"
    return 1
  fi
  echo "$NEW" > "$PIDFILE"
  chown "$RUN_USER:$RUN_USER" "$PIDFILE" 2>/dev/null
  log "已拉起新进程 pid=$NEW (user=$RUN_USER)"

  # OOM 保护：把服务调成"最后才被杀"。只有 root 能把 oom_score_adj 设成负值，
  # 所以只能在这个（root）脚本里做。必须反复扫描整棵进程树：main.py 稍后才
  # fork worker，而 oom_score_adj 是在 fork 那一刻继承的。
  if [ "$OOM_ADJ" -lt 0 ] 2>/dev/null; then
    for _ in $(seq 1 10); do
      for p in $(pgrep -f "main\.py|admin\.managed_worker" 2>/dev/null); do
        [ "$(readlink -f /proc/$p/cwd 2>/dev/null)" = "$PROJ" ] || continue
        cur=$(cat /proc/$p/oom_score_adj 2>/dev/null || echo "")
        if [ "$cur" != "$OOM_ADJ" ] && echo "$OOM_ADJ" > "/proc/$p/oom_score_adj" 2>/dev/null; then
          log "已为 pid=$p 设置 oom_score_adj=$OOM_ADJ（OOM 时最后才被杀）"
        fi
      done
      sleep 1
    done
  fi
  return 0
}

# ------------------------------ 健康判定 -----------------------------------
# 真实 HTTP 而不是"进程存在"：进程活着但事件循环卡死时 nginx 侧同样是 502。
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
       "http://127.0.0.1:$PORT/admin" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  rm -f "$FAILSTATE"
  exit 0
fi

phase=$(owner_phase)

case "$phase" in
  starting)
    log "GET /admin -> $code，但所有者正在 starting（慢启动）：跳过破坏性恢复"
    exit 0
    ;;
  draining|stopping)
    log "GET /admin -> $code，但所有者正在 $phase（主动切换）：跳过破坏性恢复"
    exit 0
    ;;
  unreachable)
    log "GET /admin -> $code，所有者状态暂不可用：不把「问不到」当成安全强杀，本次不处置"
    log "  稍后自动重试；若持续如此请人工核对：python $PROJ/main.py --status --port $PORT"
    exit 0
    ;;
  failed)
    log "GET /admin -> $code，上一代 failed：保留现场，请人工检查 $ERRLOG"
    exit 0
    ;;
esac

# 走到这里只可能是 ready（有所有者且自认就绪）或 none（没有所有者）。
n=$(( $(cat "$FAILSTATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILSTATE"
log "GET /admin -> $code（阶段 $${phase:-unknown}），连续不健康 $n 次（阈值 $THRESHOLD）"
if [ "$n" -lt "$THRESHOLD" ]; then
  exit 0
fi

if [ "$phase" = "none" ]; then
  log "确认没有运行中的所有者：拉起服务"
  start_service || exit 1
  rm -f "$FAILSTATE"
  exit 0
fi

# 所有者自认就绪但页面不可用：先请求它正常排空，只有确认退出才拉起。
log "所有者自认就绪但页面不可用：请求正常排空（不强杀）"
"$PY" "$PROJ/main.py" --stop --port "$PORT" --wait-timeout "$STOP_WAIT" >>"$LOG" 2>&1
rc=$?
case "$rc" in
  0|5)
    log "所有者已退出（rc=$rc）：拉起服务"
    rm -f "$FAILSTATE"
    start_service || exit 1
    ;;
  *)
    log "所有者没有在 $STOP_WAIT 秒内退出（rc=$rc）：保留现场，需人工确认后再处置"
    log "  查看状态：python $PROJ/main.py --status --port $PORT"
    exit 0
    ;;
esac

# ------------------------------ 确认恢复 -----------------------------------
for i in $(seq 1 20); do
  sleep 3
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
      "http://127.0.0.1:$PORT/admin" 2>/dev/null || echo 000)
  if [ "$c" = "200" ]; then
    log "重启成功（第 $i 次探测，HTTP 200）"
    exit 0
  fi
done
log "重启后仍未恢复，请人工检查"
exit 1
