import { Alert, App as AntdApp, Button, Checkbox, Input, InputNumber, Modal, Progress, Segmented, Space, Spin, Table, Tag, Typography, Upload } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { downloadText, exportName } from '../../utils/download'
import { copyText } from '../../utils/clipboard'
import { FMT_EMPTY, fmtCreditExact, fmtInt, fmtTime } from '../../utils/format'
import type { WbAccount } from './types'

/** 手动添加账号：粘贴 auth JSON */
export function ManualAddModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone?: () => void }) {
  const { message } = AntdApp.useApp()
  const [name, setName] = useState('')
  const [json, setJson] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    let auth: unknown
    try {
      auth = JSON.parse(json)
    } catch {
      message.warning('auth JSON 解析失败，请检查格式')
      return
    }
    setBusy(true)
    try {
      await api('/api/accounts', { method: 'POST', body: { name: name.trim() || undefined, auth_json: typeof auth === 'string' ? auth : JSON.stringify(auth) } })
      message.success('账号已添加')
      setJson('')
      setName('')
      onDone?.()
      onClose()
    } catch (e) {
      message.error((e as Error).message || '添加失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="手动添加账号"
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="添加"
      confirmLoading={busy}
      width={640}
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        <Input placeholder="名称（可选，默认取 uid）" value={name} onChange={(e) => setName(e.target.value)} />
        <Input.TextArea
          rows={10}
          placeholder='粘贴完整 auth JSON（从客户端导出的登录态）'
          value={json}
          onChange={(e) => setJson(e.target.value)}
        />
      </Space>
    </Modal>
  )
}

/** 导入进度状态（分批提交的实时视图） */
interface ImportProgress {
  total: number
  done: number
  added: number
  updated: number
  failed: number
  errors: string[]
  running: boolean
  cancelled: boolean
}

const mmss = (sec: number) =>
  sec >= 60 ? `${Math.floor(sec / 60)} 分 ${sec % 60} 秒` : `${sec} 秒`

/**
 * 批量导入：文本粘贴 + 文件上传（前端解析 JSON/JSONL；storage 缺省为 WorkBuddy）。
 *
 * **分批提交**：账号多时一条大请求会跑几分钟（后端逐条做真实上游验证），
 * 中途零反馈像假死、超时还会整单报错。这里把条目切片逐批 POST，
 * 进度条实时汇报、可中途取消（已导入的不回滚），失败明细留在弹窗里。
 */
export function ImportModal({
  open,
  onClose,
  onDone,
  storage = 'workbuddy',
  realm,
}: {
  open: boolean
  onClose: () => void
  onDone?: () => void
  /** 平台 storage id：workbuddy 走 /api/accounts/batch，其余走 /api/{storage}/batch */
  storage?: string
  realm?: string
}) {
  const { message } = AntdApp.useApp()
  const [text, setText] = useState('')
  const [progress, setProgress] = useState<ImportProgress | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const cancelRef = useRef(false)
  const runningRef = useRef(false)

  // 运行中每秒跳一次已耗时
  useEffect(() => {
    if (!progress?.running) return
    const timer = window.setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => window.clearInterval(timer)
  }, [progress?.running])

  /**
   * 解析导入内容为批量条目 `{name?, payload}`。
   * payload 保持**原始形态**（对象或字符串），由 submit 按平台打包：
   * WorkBuddy 的 auth_json 要求字符串；其它平台 /batch 直接收凭证对象。
   */
  const parseItems = (raw: string): { name?: string; payload: unknown }[] => {
    const items: { name?: string; payload: unknown }[] = []
    const trimmed = raw.replace(/^\uFEFF/, '').trim()
    const push = (o: Record<string, unknown>) => {
      if (o.auth_json !== undefined) {
        items.push({ ...(o.name !== undefined ? { name: o.name as string } : {}), payload: o.auth_json })
      } else {
        items.push({ payload: o })
      }
    }
    if (!trimmed) return items
    if (trimmed.startsWith('[')) {
      for (const it of JSON.parse(trimmed) as unknown[]) {
        if (!it || typeof it !== 'object') continue
        push(it as Record<string, unknown>)
      }
      return items
    }
    for (const line of trimmed.split('\n')) {
      const t = line.trim()
      if (!t) continue
      const parsed = JSON.parse(t) as Record<string, unknown>
      if (parsed && typeof parsed === 'object') push(parsed)
    }
    return items
  }

  const submit = async (items: { name?: string; payload: unknown }[]) => {
    if (!items.length) {
      message.warning('没有可导入的账号')
      return
    }
    if (runningRef.current) {
      message.warning('已有导入任务在进行中，请先等待完成或取消')
      return
    }
    const isWb = storage === 'workbuddy'
    const endpoint = isWb
      ? '/api/accounts/batch'
      : `/api/${storage}/batch${realm ? `?realm=${encodeURIComponent(realm)}` : ''}`
    // WorkBuddy 逐条真实验证较慢，批切小一点
    const batchSize = isWb ? 5 : 10
    const pack = (it: { name?: string; payload: unknown }) => {
      const named = it.name !== undefined ? { name: it.name } : {}
      return isWb
        ? { ...named, auth_json: typeof it.payload === 'string' ? it.payload : JSON.stringify(it.payload) }
        : { ...named, credential: it.payload }
    }

    cancelRef.current = false
    runningRef.current = true
    setElapsed(0)
    setProgress({ total: items.length, done: 0, added: 0, updated: 0, failed: 0, errors: [], running: true, cancelled: false })
    const acc = { added: 0, updated: 0, failed: 0, errors: [] as string[] }
    let cancelled = false

    for (let start = 0; start < items.length; start += batchSize) {
      if (cancelRef.current) {
        cancelled = true
        break
      }
      const slice = items.slice(start, start + batchSize)
      try {
        const r = await api<{ added?: number; updated?: number; errors?: { index?: number; error?: string }[] }>(
          endpoint,
          { method: 'POST', body: { items: slice.map(pack) }, timeout: 180000 },
        )
        acc.added += r.added ?? 0
        acc.updated += r.updated ?? 0
        for (const e of r.errors ?? []) {
          acc.failed += 1
          const no = start + (typeof e.index === 'number' ? e.index : 0) + 1
          acc.errors.push(`第 ${no} 条：${e.error || '未知错误'}`)
        }
      } catch (e) {
        // 单批失败不拖垮整批：记入失败明细，继续下一批
        acc.failed += slice.length
        acc.errors.push(`第 ${start + 1}-${start + slice.length} 条提交失败：${(e as Error).message || '网络错误'}`)
      }
      setProgress((p) =>
        p
          ? {
              ...p,
              done: Math.min(start + batchSize, items.length),
              added: acc.added,
              updated: acc.updated,
              failed: acc.failed,
              errors: [...acc.errors],
            }
          : p,
      )
    }

    runningRef.current = false
    setProgress((p) => (p ? { ...p, running: false, cancelled } : p))
    if (!acc.errors.length) {
      message.success(
        cancelled
          ? `已取消导入：完成 ${fmtInt(acc.added + acc.updated)} / ${fmtInt(items.length)}`
          : `导入完成：新增 ${fmtInt(acc.added)}，更新 ${fmtInt(acc.updated)}`,
      )
      setText('')
      setProgress(null)
      onDone?.()
      onClose()
    }
    // 有失败明细：弹窗留在「结果视图」，由用户看完手动关闭（见 finishClose）
  }

  const submitText = async () => {
    try {
      await submit(parseItems(text))
    } catch {
      message.warning('解析失败：请粘贴 JSON 数组或 JSONL（每行一个对象）')
    }
  }

  /** 结果视图的关闭：清状态 + 通知刷新 */
  const finishClose = () => {
    setProgress(null)
    setText('')
    onDone?.()
    onClose()
  }

  /** 运行中点右上角 ×：等价于「取消」——当前批次跑完即停，已导入的保留 */
  const handleCancel = () => {
    if (progress?.running) {
      cancelRef.current = true
      return
    }
    onClose()
  }

  const percent = progress ? Math.round((progress.done / Math.max(progress.total, 1)) * 100) : 0

  return (
    <Modal
      title="导入账号"
      open={open}
      onCancel={handleCancel}
      footer={null}
      width={680}
      maskClosable={false}
      closable={!progress?.running}
    >
      {progress ? (
        /* ---- 进度 / 结果视图 ---- */
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Progress
            percent={percent}
            status={progress.running ? 'active' : progress.failed ? 'exception' : 'success'}
          />
          <Space wrap size={16}>
            <Typography.Text>
              已导入 <span className="num">{fmtInt(progress.done)}</span> / {fmtInt(progress.total)}
            </Typography.Text>
            <Typography.Text type="secondary">
              新增 {fmtInt(progress.added)} · 更新 {fmtInt(progress.updated)}
              {progress.failed > 0 && (
                <Typography.Text type="danger"> · 失败 {fmtInt(progress.failed)}</Typography.Text>
              )}
            </Typography.Text>
            <Typography.Text type="secondary" className="num">
              已耗时 {mmss(elapsed)}
            </Typography.Text>
          </Space>
          {progress.running ? (
            <Space align="center">
              <Spin size="small" />
              <Typography.Text type="secondary">正在验证上游登录态，请勿关闭页面…</Typography.Text>
              <Button
                danger
                size="small"
                onClick={() => {
                  cancelRef.current = true
                }}
              >
                取消导入
              </Button>
            </Space>
          ) : (
            <>
              {progress.cancelled && (
                <Alert type="warning" showIcon message="已取消：当前批次完成后停止，已导入的账号保留" />
              )}
              {progress.errors.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message={`${progress.errors.length} 条失败（其余已入库）`}
                  description={
                    <div style={{ maxHeight: 160, overflow: 'auto' }}>
                      {progress.errors.slice(0, 50).map((e, i) => (
                        <Typography.Paragraph key={i} style={{ margin: 0, fontSize: 12 }}>
                          {e}
                        </Typography.Paragraph>
                      ))}
                      {progress.errors.length > 50 && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          …其余 {progress.errors.length - 50} 条略
                        </Typography.Text>
                      )}
                    </div>
                  }
                />
              )}
              <Space>
                <Button type="primary" onClick={finishClose}>
                  完成
                </Button>
                {progress.errors.length > 0 && (
                  <Button
                    onClick={() =>
                      void copyText(progress.errors.join('\n')).then((ok) =>
                        ok ? message.success('已复制失败明细') : message.error('复制失败'),
                      )
                    }
                  >
                    复制失败明细
                  </Button>
                )}
              </Space>
            </>
          )}
        </Space>
      ) : (
        /* ---- 录入视图 ---- */
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            支持粘贴 JSON 数组或 JSONL（每行一个完整登录态对象）。账号多时会分批提交并显示进度。
          </Typography.Text>
          <Input.TextArea rows={8} placeholder='{"id": "...", "tokens": {...}}' value={text} onChange={(e) => setText(e.target.value)} />
          <Space>
            <Button type="primary" onClick={() => void submitText()}>
              导入文本
            </Button>
            <Upload
              showUploadList={false}
              multiple
              beforeUpload={(file) => {
                void file
                  .text()
                  .then((t) => submit(parseItems(t)))
                  .catch(() => message.error(`${file.name} 解析失败`))
                return false
              }}
            >
              <Button>从文件导入（可多选）</Button>
            </Upload>
          </Space>
        </Space>
      )}
    </Modal>
  )
}

/** 批量导出：JSONL 下载或复制（storage 缺省为 WorkBuddy；其余平台走 /api/{storage}/export） */
export function ExportModal({
  open,
  onClose,
  selectedIds,
  storage = 'workbuddy',
  realm,
}: {
  open: boolean
  onClose: () => void
  selectedIds?: number[]
  storage?: string
  realm?: string
}) {
  const { message } = AntdApp.useApp()
  const [format, setFormat] = useState<'client' | 'backup'>('client')
  const [includeDisabled, setIncludeDisabled] = useState(false)
  const [busy, setBusy] = useState(false)

  const doExport = async (copy: boolean) => {
    setBusy(true)
    try {
      const isWb = storage === 'workbuddy'
      const q = !isWb && realm ? `?realm=${encodeURIComponent(realm)}` : ''
      const r = await api<{ items?: string[] }>(isWb ? '/api/accounts/export' : `/api/${storage}/export${q}`, {
        method: 'POST',
        body: {
          ids: selectedIds?.length ? selectedIds : undefined,
          include_disabled: includeDisabled,
          format,
        },
        raw: false,
        timeout: 120000,
      })
      // items 是凭证 JSON 原文数组 → JSONL；兜底整体序列化
      const text = Array.isArray(r?.items) ? (r.items as string[]).join('\n') : JSON.stringify(r, null, 2)
      const fname = exportName(`${storage}-accounts-${format}`, 'jsonl')
      if (copy) {
        const ok = await copyText(text)
        if (ok) message.success('已复制到剪贴板')
        else message.error('复制失败，请改用下载')
      } else {
        await downloadText(fname, text, 'application/x-ndjson')
        message.success('已下载')
      }
      onClose()
    } catch (e) {
      message.error((e as Error).message || '导出失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="导出账号" open={open} onCancel={onClose} footer={null} width={520}>
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        {storage === 'workbuddy' && (
          <Segmented
            value={format}
            onChange={(v) => setFormat(v as 'client' | 'backup')}
            options={[
              { label: '客户端格式（可再导入）', value: 'client' },
              { label: '备份格式（含完整字段）', value: 'backup' },
            ]}
          />
        )}
        <Checkbox checked={includeDisabled} onChange={(e) => setIncludeDisabled(e.target.checked)}>
          包含已停用账号
        </Checkbox>
        {selectedIds?.length ? (
          <Typography.Text type="secondary">将导出选中的 {selectedIds.length} 个账号</Typography.Text>
        ) : (
          <Typography.Text type="secondary">未勾选行时导出全部账号</Typography.Text>
        )}
        <Space>
          <Button type="primary" loading={busy} onClick={() => void doExport(false)}>
            下载 JSONL
          </Button>
          <Button loading={busy} onClick={() => void doExport(true)}>
            复制到剪贴板
          </Button>
        </Space>
      </Space>
    </Modal>
  )
}

/** 重新登录：粘贴/上传新凭证 JSON */
export function ReauthModal({ account, onClose, onDone }: { account: WbAccount | null; onClose: () => void; onDone?: () => void }) {
  const { message } = AntdApp.useApp()
  const [json, setJson] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!account) setJson('')
  }, [account])

  const submit = async () => {
    if (!account) return
    let auth: unknown
    try {
      auth = JSON.parse(json)
    } catch {
      message.warning('JSON 解析失败')
      return
    }
    setBusy(true)
    try {
      await api(`/api/accounts/${account.id}/reauth`, { method: 'POST', body: { auth_json: typeof auth === 'string' ? auth : JSON.stringify(auth) } })
      message.success('凭证已更新')
      onDone?.()
      onClose()
    } catch (e) {
      message.error((e as Error).message || '更新失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`重新登录：${account?.name ?? ''}`}
      open={!!account}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="更新凭证"
      confirmLoading={busy}
      width={640}
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        <Upload
          showUploadList={false}
          maxCount={1}
          beforeUpload={(f) => {
            void f.text().then((t) => setJson(t.slice(0, 1024 * 1024)))
            return false
          }}
        >
          <Button>从文件读取（≤1MB）</Button>
        </Upload>
        <Input.TextArea rows={10} placeholder="粘贴新的 auth JSON" value={json} onChange={(e) => setJson(e.target.value)} />
      </Space>
    </Modal>
  )
}

/** 并发上限调整 */
export function ConcurrencyModal({ account, onClose, onDone }: { account: WbAccount | null; onClose: () => void; onDone?: () => void }) {
  const { message } = AntdApp.useApp()
  const [val, setVal] = useState<number | null>(null)

  useEffect(() => {
    if (account) setVal(account.max_in_flight ?? null)
  }, [account])

  const submit = async () => {
    if (!account) return
    try {
      await api(`/api/accounts/${account.id}`, { method: 'PATCH', body: { max_in_flight: val } })
      message.success('并发上限已更新')
      onDone?.()
      onClose()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <Modal
      title={`并发上限：${account?.name ?? ''}`}
      open={!!account}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="保存"
      width={420}
    >
      <InputNumber
        style={{ width: '100%' }}
        min={0}
        max={64}
        value={val}
        onChange={setVal}
        suffix="（0 = 使用全局默认）"
      />
    </Modal>
  )
}

/** 健康检查结果弹窗（三段：积分 / 模型列表 / 真实对话） */
export function HealthCheckModal({ result, onClose }: { result: Record<string, unknown> | null; onClose: () => void }) {
  const checks = (result?.checks ?? result?.items ?? result?.stages ?? []) as { name?: string; ok?: boolean; message?: string; detail?: string }[]
  return (
    <Modal title={`健康检查：${(result?.account_name as string) || ''}`} open={!!result} onCancel={onClose} footer={null} width={560}>
      {checks.length ? (
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          {checks.map((c, i) => (
            <Alert
              key={i}
              type={c.ok ? 'success' : 'error'}
              showIcon
              title={c.name || c.message || `检查 ${i + 1}`}
              description={c.detail || (c.ok ? undefined : c.message)}
            />
          ))}
        </Space>
      ) : (
        <Typography.Paragraph code style={{ whiteSpace: 'pre-wrap' }}>
          {JSON.stringify(result, null, 2)}
        </Typography.Paragraph>
      )}
    </Modal>
  )
}

/** 积分明细弹窗 */
export function CreditDetailsModal({ account, onClose }: { account: WbAccount | null; onClose: () => void }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!account) return
    setLoading(true)
    api<Record<string, unknown>>(`/api/accounts/${account.id}/credit-details`)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [account])

  const packages = (data?.packages ?? data?.items ?? []) as Record<string, unknown>[]
  return (
    <Modal title={`积分明细：${account?.name ?? ''}`} open={!!account} onCancel={onClose} footer={null} width={680}>
      {loading ? (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      ) : (
        <Table
          size="small"
          rowKey={(_, i) => String(i)}
          dataSource={packages}
          pagination={{ pageSize: 10 }}
          columns={[
            { title: '名称', dataIndex: 'display_name', ellipsis: true },
            { title: '剩余', dataIndex: 'remaining', width: 100, align: 'right', render: (v) => <span className="num">{fmtCreditExact(v)}</span> },
            { title: '总额', dataIndex: 'total', width: 100, align: 'right', render: (v) => <span className="num">{fmtCreditExact(v)}</span> },
            { title: '到期', dataIndex: 'expire_label', width: 120, render: (v, r) => <Tag color={r.expired ? 'error' : r.expiring_soon ? 'warning' : 'default'}>{v || FMT_EMPTY}</Tag> },
          ]}
        />
      )}
    </Modal>
  )
}

/** 上游请求用量弹窗（动态列） */
export function RequestUsageModal({ account, onClose }: { account: WbAccount | null; onClose: () => void }) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!account) return
    setLoading(true)
    api<{ items?: Record<string, unknown>[] }>(`/api/accounts/${account.id}/request-usage?page_num=1&page_size=50`)
      .then((r) => setRows(r.items ?? []))
      .catch(() => setRows([]))
      .finally(() => setLoading(false))
  }, [account])

  const cols = rows[0]
    ? Object.keys(rows[0])
        .filter((k) => typeof rows[0][k] !== 'object')
        .slice(0, 8)
        .map((k) => ({
          title: k,
          dataIndex: k,
          ellipsis: true,
          render: (v: unknown) => String(v ?? FMT_EMPTY),
        }))
    : []

  return (
    <Modal title={`上游请求用量：${account?.name ?? ''}`} open={!!account} onCancel={onClose} footer={null} width={860}>
      <Table size="small" rowKey={(_, i) => String(i)} loading={loading} dataSource={rows} pagination={{ pageSize: 10 }} columns={cols} />
    </Modal>
  )
}

/** 模型额度与冷却诊断（对应旧版 openModelLimits） */
export function ModelLimitsModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone?: () => void }) {
  const { message } = AntdApp.useApp()
  const [data, setData] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  const load = () => {
    setLoading(true)
    Promise.all([api<Record<string, unknown>>('/api/accounts/model-limits'), api<Record<string, unknown>>('/api/stats/model-health?hours=24')])
      .then(([a, h]) => setData({ limits: a, health: h }))
      .catch((e) => message.error((e as Error).message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (open) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const items = ((data?.limits as { items?: Record<string, unknown>[] })?.items ?? []) as Array<{
    account_id?: number
    account_name?: string
    model?: string
    kind?: string
    until?: string
    remaining_seconds?: number
    upstream_error?: { http_status?: number; code?: string; client_message?: string; message?: string; display_message?: string }
    [k: string]: unknown
  }>

  const clear = async (accountId: number | undefined, model: string) => {
    if (!accountId) return
    try {
      await api(`/api/accounts/${accountId}/model-limits/clear`, { method: 'POST', body: { model } })
      message.success('已解除限制')
      load()
      onDone?.()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <Modal title="模型额度与冷却诊断" open={open} onCancel={onClose} footer={null} width={860}>
      {loading ? (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      ) : (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            WAF 剩余：{fmtInt((data?.limits as { waf_remaining_seconds?: number })?.waf_remaining_seconds)} 秒
          </Typography.Paragraph>
          <Table
            size="small"
            rowKey={(r, i) => `${r.account_id}-${r.model}-${i}`}
            dataSource={items}
            pagination={{ pageSize: 10 }}
            columns={[
              { title: '账号', dataIndex: 'account_name', ellipsis: true },
              { title: '模型', dataIndex: 'model', width: 160, ellipsis: true },
              { title: '类型', dataIndex: 'kind', width: 110 },
              { title: '恢复', dataIndex: 'until', width: 140, render: (v, r) => ((r.remaining_seconds ?? 0) > 0 ? `${Math.ceil((r.remaining_seconds ?? 0) / 60)} 分钟后` : fmtTime(v)) },
              {
                title: '上游证据',
                ellipsis: true,
                render: (_, r) => {
                  const ue = r.upstream_error
                  if (!ue) return FMT_EMPTY
                  return (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      HTTP {ue.http_status ?? FMT_EMPTY} · {ue.code || FMT_EMPTY} · {ue.client_message || ue.display_message || ue.message || FMT_EMPTY}
                    </Typography.Text>
                  )
                },
              },
              {
                title: '操作',
                width: 80,
                render: (_, r) => (
                  <Button size="small" type="link" onClick={() => void clear(r.account_id, r.model ?? '')}>
                    解除
                  </Button>
                ),
              },
            ]}
          />
        </>
      )}
    </Modal>
  )
}
