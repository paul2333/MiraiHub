"""计划升级窗口的发布侧 CLI：标记 → 旧代 hold（封口+排空）→ 提交停止 → 新代就绪 → 结束标记。

分工：
  * 本脚本只写「升级标记」并通过本机生命周期 IPC 驱动**旧代**，不直接杀进程；
  * 标记（默认 /run/wb2api-upgrade/active）有效期间，nginx 与 scripts/upgrade_gate.py
    把新请求排进队列，客户端看到的是等待心跳而不是 502；
  * 旧代收到 BEGIN(action="upgrade_hold")：封口、排空，归零后**保持可查询**，
    只有本脚本 commit 才把同一 operation_id 从 hold 升级为 stop；
  * 预算耗尽时放弃本次**发布**：同代 RESUME → 确认旧代 ready → 结束标记，
    排队请求回到旧代继续接单。

为什么 drain 不再读 /api/logs/requests 的 active_count：
  那张表只统计“推理路径”，音频、后台任务、审计收尾都不在里面；
  用它的 0 判“可以安全切换”会截断正在收尾的工作。归零只信 worker 在锁内
  返回的 active_requests / active_jobs。

退出码：
  0 成功；1 运行错误；2 用法；3 旧版能力（缺 IPC / readiness 探针，只能维护窗口
  人工切换）；4 预算耗尽且已撤销发布；5 操作号/状态/标记冲突。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

READY_PATH = '/api/app/ready'
HEALTH_PATH = '/__upgrade_health'
HOLD_SUFFIX = '.hold.json'
MAX_SECONDS = 180
RESET_RESERVE_SECONDS = 30
RESUME_ATTEMPTS = 3
DEFAULT_CONTROL_DIR = '/run/wb2api-lifecycle'

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LEGACY = 3
EXIT_BUDGET = 4
EXIT_CONFLICT = 5


class LegacyCapability(RuntimeError):
    """本代无法自动切换：缺生命周期 IPC、缺密钥或后端没有 readiness 探针。

    这类部署只允许维护窗口人工切换 —— 外部关闭业务入口、确认旧实例空闲，
    绝不能把“探针 404”当成“已经就绪”。
    """


def judge_ready(response):
    """readiness 判定：只认 HTTP 200 + JSON 且 ready 为布尔真值。

    非 JSON（网关 HTML 错误页）、字符串 "true"、404（旧镜像没有这个接口）
    全部算未就绪。
    """
    if response is None or response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get('ready') is True


def hold_is_valid(hold_until, now=None):
    """未提交 hold 的租期与升级标记一致（epoch 秒）；过期即视为不再受保护。"""
    if not hold_until:
        return False
    try:
        limit = float(hold_until)
    except (TypeError, ValueError):
        return False
    return limit > (time.time() if now is None else now)


def multi_worker_error(workers):
    """多 worker 部署没有“逐个封口并收齐回执”的实现，必须明确拒绝本次自动切换。"""
    if workers <= 1:
        return None
    return ('多 worker 部署不支持本次自动切换：一次 drain 只能证明一个进程的计数归零，'
            '不能把一个进程的 0 当作全站的 0。请改为单 worker（admin.managed_worker），'
            '或在维护窗口内人工切换。')


def read_marker(marker):
    try:
        return float(Path(marker).read_text(encoding='ascii'))
    except (OSError, ValueError):
        return None


def write_marker(marker, expires):
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix('.new')
    temporary.write_text(str(expires), encoding='ascii')
    os.chmod(temporary, 0o644)
    temporary.replace(marker)


def state_path(marker):
    marker = Path(marker)
    return marker.with_name(marker.name + HOLD_SUFFIX)


def read_state(marker):
    try:
        return json.loads(state_path(marker).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_state(marker, state):
    """hold 状态与标记放同一目录，权限 0600：里面只有操作号/实例号，没有凭据。"""
    target = state_path(marker)
    temporary = target.with_suffix('.new')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def clear_state(marker):
    state_path(marker).unlink(missing_ok=True)


def _decode_key(raw):
    """十六进制写法优先（runner 注入的那种），不是 hex 就按原始字节用。"""
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError:
        return raw
    if text and len(text) % 2 == 0 and all(char in '0123456789abcdefABCDEF' for char in text):
        return bytes.fromhex(text)
    return raw


def authkey(args):
    """authkey 只从受保护文件或进程环境取：命令行参数会进 ps 与 shell 历史。"""
    if args.authkey_file:
        try:
            return _decode_key(Path(args.authkey_file).read_bytes().strip())
        except OSError:
            return None
    value = os.getenv('WB_LIFECYCLE_AUTHKEY')
    if value:
        return _decode_key(value.strip().encode('utf-8'))
    return None


def owner(args):
    """按契约第 3/4 节连到本机 worker 的生命周期 IPC。"""
    try:
        from admin.lifecycle_control import WorkerOwner
    except ModuleNotFoundError as exc:      # 旧镜像/未部署 managed runner
        raise LegacyCapability('本代没有 admin.lifecycle_control，生命周期 IPC 不可用') from exc
    key = authkey(args)
    if not key:
        raise LegacyCapability('找不到生命周期 IPC 的 authkey（--authkey-file 或 WB_LIFECYCLE_AUTHKEY）')
    control_dir = Path(args.control_dir or os.getenv('WB_LIFECYCLE_CONTROL_DIR')
                       or DEFAULT_CONTROL_DIR)
    return WorkerOwner(control_dir, key)


def open_hold(worker, operation_id, hold_until):
    """BEGIN(action="upgrade_hold")：封口 + 等归零，但零之后仍可查询。

    租期（hold_until）用同名字段附带给支持它的实现；旧实现忽略该字段时，
    以标记到期为准（标记最多 180 秒，不会永久封口）。
    """
    try:
        return worker.begin_upgrade_hold(operation_id, hold_until=hold_until)
    except TypeError:
        return worker.begin_upgrade_hold(operation_id)


def counts(snapshot):
    if not isinstance(snapshot, dict):
        return 0, 0
    try:
        return int(snapshot.get('active_requests') or 0), int(snapshot.get('active_jobs') or 0)
    except (TypeError, ValueError):
        return 0, 0


def is_idle(snapshot):
    """归零判定：worker 自己保证的两种计数都为 0，且阶段可判别。

    控制通道失联（unreachable）、已停止（stopped）、启动中（starting）都不算空闲 ——
    “暂时观测不到”绝不等于“排空完成”。
    """
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get('phase') not in ('draining', 'stopping'):
        return False
    requests, jobs = counts(snapshot)
    return requests == 0 and jobs == 0


def wait_until_zero(worker, deadline, log=print, interval=.5, heartbeat=5.0):
    """流式等待心跳：剩余计数变化或每 5 秒打一行，归零即返回快照。"""
    started = time.monotonic()
    last = None
    last_at = 0.0
    while time.monotonic() < deadline:
        snapshot = worker.status()
        if is_idle(snapshot):
            return snapshot
        requests, jobs = counts(snapshot)
        now = time.monotonic()
        if (requests, jobs) != last or now - last_at >= heartbeat:
            last, last_at = (requests, jobs), now
            log('排空中：请求 {0} 个、后台任务 {1} 个，已等 {2:.0f} 秒'.format(
                requests, jobs, now - started))
        time.sleep(interval)
    raise TimeoutError('排空预算耗尽')


def resume_old_generation(worker, state, log=print):
    """撤销未提交的 hold：同代 RESUME；IPC 短暂失联按小退避重试。"""
    operation_id = state.get('operation_id') or ''
    instance_id = state.get('instance_id') or ''
    error = None
    for attempt in range(RESUME_ATTEMPTS):
        try:
            return worker.resume(operation_id, instance_id)
        except Exception as exc:            # 连接/协议错误都只意味着“暂时观测不到”
            error = exc
            log('RESUME 第 {0} 次失败：{1}'.format(attempt + 1, exc))
            time.sleep(.5 * (attempt + 1))
    raise RuntimeError('RESUME 失败：{0}'.format(error))


def probe_ready(client, backend):
    """返回 (是否就绪, HTTP 状态码)；连不上时状态码为 None。"""
    try:
        response = client.get(backend + READY_PATH)
    except httpx.HTTPError:
        return False, None
    return judge_ready(response), response.status_code


def read_health(client, gate):
    """等待代理健康：只用于判断窗口是否已开、队列是否非空。"""
    try:
        response = client.get(gate + HEALTH_PATH)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {'ok': False}
    if response.status_code != 200 or not isinstance(payload, dict):
        return {'ok': False}
    return payload


def abandon(worker, marker, state, client, backend, log=print):
    """放弃本次发布：同代 RESUME → 确认旧代 ready → 结束标记。

    顺序不能反：先撤标记再确认，会出现“标记没了但旧代还封着”的空窗。
    """
    try:
        snapshot = resume_old_generation(worker, state, log=log)
    except RuntimeError as exc:
        log('{0}；保持标记与 hold'.format(exc))
        log('旧代会在核实仍是同一未提交 hold 后自动恢复准入，标记最多 180 秒后自行过期')
        return EXIT_ERROR
    log('旧代已恢复准入（instance_id={0}）'.format(snapshot.get('instance_id') if isinstance(snapshot, dict) else '未知'))
    ready, status = probe_ready(client, backend)
    if not ready:
        log('旧代尚未确认 ready（HTTP {0}）：暂不撤标记，稍后重试 abort/drain'.format(status))
        return EXIT_ERROR
    clear_state(marker)
    Path(marker).unlink(missing_ok=True)
    log('已结束升级标记，排队请求回到旧代')
    return EXIT_OK


def mark_legacy(marker, operation_id, expires, reason, log=print):
    """旧版能力：保留标记（排队仍在生效），只报告限制，不假装能自动切换。"""
    write_state(marker, {'operation_id': operation_id, 'instance_id': None,
                         'hold_until': expires, 'legacy': True, 'committed': False})
    log('旧版能力：{0}'.format(reason))
    log('本次只开启等待窗口（最多 {0} 秒）：自动化封口/排空不可用，'
        '只允许在外部关闭业务入口、确认旧实例空闲的维护窗口切换。'.format(MAX_SECONDS))
    log('不要把这个窗口当成“可以安全切流量”的证明；旧镜像的探针 404 不等于就绪。')
    return EXIT_LEGACY


def cmd_begin(args, marker, client, log=print):
    if not 1 <= args.seconds <= MAX_SECONDS:
        raise SystemExit('--seconds 必须在 1 到 {0} 之间'.format(MAX_SECONDS))
    if Path(marker).exists():
        raise SystemExit('升级标记已存在：{0}（先 end/abort，或等它过期）'.format(marker))
    health = read_health(client, args.gate)
    if not health.get('ok'):
        raise SystemExit('等待代理不可达（{0}）：先确认 upgrade gate 在跑'.format(args.gate))
    if health.get('upgrading') or health.get('pending'):
        raise SystemExit('已有升级窗口或排队请求，先把上一轮处理完再 begin')
    operation_id = 'upg-' + uuid.uuid4().hex
    expires = time.time() + args.seconds
    # 顺序固定：先开标记（新请求开始排队），再给旧代发 hold。
    write_marker(marker, expires)
    log('升级标记已开启：{0} 秒后过期（operation_id={1}）'.format(args.seconds, operation_id))
    try:
        worker = owner(args)
        snapshot = open_hold(worker, operation_id, expires)
    except LegacyCapability as exc:
        return mark_legacy(marker, operation_id, expires, str(exc), log=log)
    except Exception as exc:
        Path(marker).unlink(missing_ok=True)
        log('BEGIN(upgrade_hold) 失败：{0}；已关闭升级标记，本次发布未开始'.format(exc))
        return EXIT_ERROR
    instance_id = snapshot.get('instance_id') if isinstance(snapshot, dict) else None
    write_state(marker, {'operation_id': operation_id, 'instance_id': instance_id,
                         'hold_until': expires, 'legacy': False, 'committed': False})
    requests, jobs = counts(snapshot)
    log('旧代已封口（instance_id={0}）：在途请求 {1} 个、后台任务 {2} 个；'
        '归零后仍可查询，等 commit 才会停止'.format(instance_id, requests, jobs))
    return EXIT_OK


def cmd_drain(args, marker, client, log=print):
    if not 0 <= args.seconds <= MAX_SECONDS:
        raise SystemExit('--seconds 必须在 0 到 {0} 之间'.format(MAX_SECONDS))
    state = read_state(marker)
    if state is None:
        raise SystemExit('没有 hold 记录：先 begin（升级标记不是“已封口”的证明）')
    if state.get('committed'):
        raise SystemExit('本次操作已提交停止，回退已禁用；失败恢复走“恢复旧代码 → 验证 ready → end”')
    if state.get('legacy'):
        raise SystemExit('旧版能力：本代没有生命周期 IPC，无法证明全站在途为 0；'
                         '只允许在外部关闭业务入口、确认旧实例空闲的维护窗口切换')
    hold_until = state.get('hold_until') or read_marker(marker)
    if not hold_is_valid(hold_until):
        raise SystemExit('升级标记已过期，hold 租期随之结束；先 end 再重新 begin')
    try:
        remaining = float(hold_until) - time.time() - RESET_RESERVE_SECONDS
    except (TypeError, ValueError):
        raise SystemExit('hold 记录的租期不可解析：先 end 再重新 begin')
    budget = min(float(args.seconds), remaining)
    deadline = time.monotonic() + max(0.0, budget)
    try:
        worker = owner(args)
    except LegacyCapability as exc:
        raise SystemExit('旧版能力：{0}'.format(exc))
    try:
        snapshot = wait_until_zero(worker, deadline, log=log)
    except TimeoutError:
        log('排空预算耗尽：放弃本次发布，让排队请求回到旧代')
        code = abandon(worker, marker, state, client, args.backend, log=log)
        # 撤销成功也仍是“本次发布没做成”，用独立退出码告诉调用方，别当成正常完成。
        return EXIT_BUDGET if code == EXIT_OK else code
    requests, jobs = counts(snapshot)
    log('旧代已归零（请求 {0}、后台任务 {1}）：仍保持封口但可查询'.format(requests, jobs))
    log('确认可以切流量后执行 commit --operation-id {0}'.format(state.get('operation_id')))
    return EXIT_OK


def cmd_commit(args, marker, client, log=print):
    state = read_state(marker)
    if state is None:
        raise SystemExit('没有 hold 记录：先 begin + drain')
    if state.get('legacy'):
        raise SystemExit('旧版能力：没有可提交的 hold，只允许维护窗口人工切换')
    operation_id = args.operation_id or state.get('operation_id')
    if operation_id != state.get('operation_id'):
        log('操作号不匹配：本次 hold 是 {0}，收到 {1}'.format(state.get('operation_id'), operation_id))
        return EXIT_CONFLICT
    if state.get('committed'):
        log('本次操作已提交停止（幂等）：instance_id={0}'.format(state.get('instance_id')))
        return EXIT_OK
    hold_until = state.get('hold_until') or read_marker(marker)
    if not hold_is_valid(hold_until):
        log('升级标记已过期：hold 租期结束，拒绝提交（先 end 再重新 begin）')
        return EXIT_CONFLICT
    try:
        worker = owner(args)
    except LegacyCapability as exc:
        raise SystemExit('旧版能力：{0}'.format(exc))
    snapshot = worker.status()
    if not is_idle(snapshot):
        requests, jobs = counts(snapshot)
        log('仍有在途（请求 {0}、后台任务 {1}）或状态不可判别：不能提交停止'.format(requests, jobs))
        return EXIT_CONFLICT
    instance_id = state.get('instance_id') or ''
    try:
        # 同一 operation_id 从 hold 升级为 stop：一次执行，不做第二次 BEGIN。
        result = worker.begin('stop', operation_id, instance_id)
    except Exception as exc:
        log('提交停止失败：{0}；hold 仍在，可重试 commit 或 abort 回到旧代'.format(exc))
        return EXIT_ERROR
    if isinstance(result, dict):
        if result.get('operation_id') not in (None, operation_id):
            log('提交回执的操作号不一致：{0}'.format(result.get('operation_id')))
            return EXIT_CONFLICT
        if instance_id and result.get('instance_id') not in (None, instance_id):
            log('提交回执的实例号不一致：{0}'.format(result.get('instance_id')))
            return EXIT_CONFLICT
    state['committed'] = True
    state['committed_at'] = time.time()
    write_state(marker, state)
    log('已提交停止：instance_id={0} operation_id={1}'.format(instance_id, operation_id))
    log('回退已禁用：接下来由平台/容器替换进程；失败恢复走“恢复旧代码 → 验证 ready → end”')
    return EXIT_OK


def cmd_abort(args, marker, client, log=print):
    state = read_state(marker)
    if state is None:
        Path(marker).unlink(missing_ok=True)
        log('没有 hold 记录：已删除陈旧的升级标记')
        return EXIT_OK
    if state.get('committed'):
        log('停止已提交，回退已禁用：恢复走“恢复旧代码 → 验证 GET {0} → end”'.format(READY_PATH))
        return EXIT_CONFLICT
    if state.get('legacy'):
        clear_state(marker)
        Path(marker).unlink(missing_ok=True)
        log('旧版能力：没有 hold 需要撤销，已结束升级标记')
        return EXIT_OK
    try:
        worker = owner(args)
    except LegacyCapability as exc:
        raise SystemExit('旧版能力：{0}'.format(exc))
    return abandon(worker, marker, state, client, args.backend, log=log)


def cmd_end(args, marker, client, log=print):
    ready, status = probe_ready(client, args.backend)
    if not ready:
        if status in (404, 405):
            log('旧版能力：后端没有 GET {0}（HTTP {1}），无法证明新实例就绪'.format(READY_PATH, status))
            log('标记最多 180 秒后自行过期；确认新实例可用后，也可手动删除 {0}'.format(marker))
            return EXIT_LEGACY
        log('新代尚未就绪（HTTP {0}）：不改动标记，稍后重试 end'.format(status))
        return EXIT_ERROR
    clear_state(marker)
    Path(marker).unlink(missing_ok=True)
    log('新代 ready：已结束升级标记，排队请求释放')
    return EXIT_OK


def cmd_status(args, marker, client, log=print):
    held = read_state(marker)
    ready, status = probe_ready(client, args.backend)
    payload = {
        'gate': read_health(client, args.gate),
        'marker': {
            'present': Path(marker).exists(),
            'expires': read_marker(marker),
            'hold': held,
        },
        'backend': {'ready': ready, 'status': status},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


ACTIONS = {
    'begin': cmd_begin,
    'drain': cmd_drain,
    'commit': cmd_commit,
    'abort': cmd_abort,
    'end': cmd_end,
    'status': cmd_status,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=sorted(ACTIONS))
    parser.add_argument('--seconds', type=int, default=180,
                        help='升级窗口/排空预算，最大 180 秒（与标记租期一致）')
    parser.add_argument('--marker', default='/run/wb2api-upgrade/active')
    parser.add_argument('--gate', default='http://127.0.0.1:8791')
    parser.add_argument('--backend', default='http://127.0.0.1:8790')
    parser.add_argument('--control-dir', default=None,
                        help='managed runner 的生命周期 IPC 目录（默认取 WB_LIFECYCLE_CONTROL_DIR）')
    parser.add_argument('--authkey-file', default=None,
                        help='生命周期 IPC 的 authkey 文件（不要放命令行，会进 ps）')
    parser.add_argument('--operation-id', default=None, help='校验/提交指定的操作号')
    parser.add_argument('--workers', type=int, default=1,
                        help='部署的 worker 数；>1 时本工具明确拒绝自动切换')
    args = parser.parse_args(argv)
    marker = Path(args.marker)
    blocked = multi_worker_error(args.workers)
    if blocked:
        print(blocked, file=sys.stderr)
        return EXIT_LEGACY
    with httpx.Client(trust_env=False, timeout=5) as client:
        if args.action in ('begin', 'drain'):
            print('开始预排空：先让新请求进等待代理，再封口旧代')
        try:
            return ACTIONS[args.action](args, marker, client)
        except SystemExit as exc:          # 用例前置条件不满足（不是 argparse 的用法错误）
            print(str(exc.code), file=sys.stderr)
            return EXIT_ERROR
        except LegacyCapability as exc:
            print('旧版能力：{0}'.format(exc), file=sys.stderr)
            return EXIT_LEGACY


if __name__ == '__main__':
    raise SystemExit(main())
