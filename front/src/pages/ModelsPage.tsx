import { App as AntdApp, Button, Card, Input, Popconfirm, Segmented, Select, Space, Switch, Table, Tag, Tooltip, Typography } from 'antd'
import { CopyOutlined, SyncOutlined } from '@ant-design/icons'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { usePlatform } from '../context/PlatformContext'
import PageHeader from '../components/PageHeader'
import { copyText } from '../utils/clipboard'
import { FMT_EMPTY, fmtDuration, fmtInt, fmtPct } from '../utils/format'

interface ModelItem {
  id: number
  level: string
  platform: string
  realm?: string
  platform_label: string
  model_id: string
  upstream_id: string
  caps?: Record<string, unknown>
  enabled: boolean
  note: string
  description?: string
  billing_mode: string
  credit_multiplier: number | null
  is_free: boolean
  official_visible?: boolean
  tags?: string[]
}

interface AggItem {
  key: string
  name: string
  platform_count: number
  min_multiplier: number | null
  free_any: boolean
  enabled_any: boolean
  model_ids: string[]
  platforms: { platform: string; model_id: string; display_name?: string; multiplier?: number; description?: string; note?: string; enabled: boolean; platform_label: string }[]
}

interface HealthRow {
  model: string
  requests: number
  successes: number
  failures: number
  success_rate: number | null
  ttfb_ms?: { p50?: number; p95?: number }
  latency_ms?: { p50?: number; p95?: number }
  available_accounts?: number
  eligible_accounts?: number
}

interface BusyRow {
  model_id: string
  busy_level: string
  busy_label: string
}

const BUSY_TONE: Record<string, string> = {
  idle: 'default',
  normal: 'success',
  busy: 'warning',
  congested: 'error',
  unavailable: 'error',
  unknown: 'default',
}

export default function ModelsPage() {
  const { message, modal } = AntdApp.useApp()
  const { platformKey } = usePlatform()
  const [items, setItems] = useState<ModelItem[]>([])
  const [aggItems, setAggItems] = useState<AggItem[]>([])
  const [health, setHealth] = useState<Record<string, HealthRow>>({})
  const [busy, setBusy] = useState<Record<string, BusyRow>>({})
  const [display, setDisplay] = useState<{ show_multiplier: boolean; show_busy: boolean; official_only: boolean }>({ show_multiplier: true, show_busy: true, official_only: false })
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState<'flat' | 'aggregate'>('flat')
  const [platformFilter, setPlatformFilter] = useState<string | undefined>()
  const [search, setSearch] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const qs = new URLSearchParams()
      if (platformFilter) qs.set('platform', platformFilter)
      const [cfg, disp, h] = await Promise.all([
        api<{ items: ModelItem[] }>(`/api/models/configs?${qs.toString()}`),
        api<typeof display>('/api/models/display'),
        api<{ items: HealthRow[] }>('/api/stats/model-health?hours=24'),
      ])
      setItems(cfg.items ?? [])
      setDisplay(disp)
      setHealth(Object.fromEntries((h.items ?? []).map((r) => [r.model, r])))
      if (view === 'aggregate') {
        const a = await api<{ items: AggItem[] }>('/api/models/aggregate')
        setAggItems(a.items ?? [])
      }
    } catch (e) {
      message.error((e as Error).message || '模型列表加载失败')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platformFilter, view, platformKey])

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load])

  // 繁忙度 50s 轮询
  useEffect(() => {
    const fetchBusy = () =>
      api<{ items: BusyRow[] }>('/api/models/busy')
        .then((r) => setBusy(Object.fromEntries((r.items ?? []).map((b) => [b.model_id, b]))))
        .catch(() => {})
    void fetchBusy()
    const timer = window.setInterval(() => {
      if (!document.hidden) void fetchBusy()
    }, 50000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleEnabled = async (m: ModelItem) => {
    try {
      await api(`/api/models/${m.id}`, { method: 'PATCH', body: { enabled: !m.enabled } })
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const removeModel = async (m: ModelItem) => {
    try {
      await api(`/api/models/${m.id}`, { method: 'DELETE' })
      message.success('已移除')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const syncModels = async () => {
    message.loading({ content: '正在从上游同步模型…', key: 'msync', duration: 0 })
    try {
      const r = await api<{ added?: number; updated?: number; total_in_db?: number; enabled?: number }>('/api/models/sync', { method: 'POST', body: {}, timeout: 120000 })
      message.success({ content: `同步完成：新增 ${fmtInt(r.added)}，更新 ${fmtInt(r.updated)}`, key: 'msync' })
      void load()
    } catch (e) {
      message.error({ content: (e as Error).message || '同步失败', key: 'msync' })
    }
  }

  const batchToggle = async (enabled: boolean) => {
    modal.confirm({
      title: enabled ? '启用全部模型？' : '停用全部模型？',
      onOk: async () => {
        try {
          await api('/api/models/batch-toggle', { method: 'POST', body: { enabled, level: 'system' } })
          message.success('已批量更新')
          void load()
        } catch (e) {
          message.error((e as Error).message)
        }
      },
    })
  }

  const saveDisplay = async (patch: Partial<typeof display>) => {
    try {
      const r = await api<typeof display>('/api/models/display', { method: 'PUT', body: { ...display, ...patch } })
      setDisplay(r)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const speedTest = async (m: ModelItem) => {
    message.loading({ content: `正在测速 ${m.model_id}…`, key: `st${m.id}`, duration: 0 })
    try {
      const r = await api<{ ttft_ms?: number; speed_tps?: number; overall_tps?: number; completion_tokens?: number; gen_duration_ms?: number; account?: string; health?: string }>(
        '/api/models/speed-test',
        { method: 'POST', body: { model: m.upstream_id || m.model_id }, timeout: 180000 },
      )
      message.success({ content: '测速完成', key: `st${m.id}` })
      modal.info({
        title: `测速结果：${m.model_id}`,
        content: (
          <Space orientation="vertical" size={4}>
            <Typography.Text>首字延迟：<span className="num">{r.ttft_ms ? `${Math.round(r.ttft_ms)}ms` : FMT_EMPTY}</span></Typography.Text>
            <Typography.Text>生成速度：<span className="num">{r.speed_tps ? `${r.speed_tps.toFixed(1)} tok/s` : FMT_EMPTY}</span></Typography.Text>
            <Typography.Text>整体速度：<span className="num">{r.overall_tps ? `${r.overall_tps.toFixed(1)} tok/s` : FMT_EMPTY}</span></Typography.Text>
            <Typography.Text>生成耗时：<span className="num">{fmtDuration(r.gen_duration_ms)}</span> · 输出 <span className="num">{fmtInt(r.completion_tokens)}</span> tokens</Typography.Text>
            {r.account && <Typography.Text type="secondary" style={{ fontSize: 12 }}>使用账号：{r.account}</Typography.Text>}
          </Space>
        ),
        width: 480,
      })
    } catch (e) {
      message.error({ content: (e as Error).message || '测速失败', key: `st${m.id}` })
    }
  }

  const platformOptions = useMemo(() => {
    const set = new Map<string, string>()
    items.forEach((m) => set.set(m.platform, m.platform_label))
    return [...set.entries()].map(([v, l]) => ({ value: v, label: l }))
  }, [items])

  const flatRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = items
    if (q) list = list.filter((m) => m.model_id.toLowerCase().includes(q) || (m.note ?? '').toLowerCase().includes(q))
    return list
  }, [items, search])

  const aggRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = aggItems
    if (q) list = list.filter((m) => m.name.toLowerCase().includes(q))
    return list
  }, [aggItems, search])

  const multiplierRender = (m: { credit_multiplier: number | null; is_free: boolean; billing_mode: string }) => {
    if (!display.show_multiplier) return FMT_EMPTY
    if (m.is_free) return <Tag color="success">FREE</Tag>
    if (m.credit_multiplier === null) return FMT_EMPTY
    if (m.credit_multiplier < 0) return <Tag>无量纲</Tag>
    if (m.credit_multiplier === 0) return <Tag color="success">FREE</Tag>
    return <span className="num">×{m.credit_multiplier}</span>
  }

  const healthRender = (modelId: string) => {
    const h = health[modelId]
    if (!h) return FMT_EMPTY
    return (
      <Tooltip title={`请求 ${fmtInt(h.requests)} · 成功 ${fmtInt(h.successes)} · 失败 ${fmtInt(h.failures)} · P50 ${h.latency_ms?.p50 ? `${Math.round(h.latency_ms.p50)}ms` : '—'}`}>
        <span className="num" style={{ fontSize: 12 }}>
          {fmtPct(h.success_rate, 0)} · {fmtInt(h.requests)}
        </span>
      </Tooltip>
    )
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <PageHeader
        title="模型配置"
        onRefresh={() => void load()}
        refreshing={loading}
        actions={
          <Space wrap>
            <Segmented value={view} onChange={(v) => setView(v as 'flat' | 'aggregate')} options={[{ label: '平台视图', value: 'flat' }, { label: '聚合视图', value: 'aggregate' }]} />
            <Button icon={<SyncOutlined />} onClick={() => void syncModels()}>
              从后端同步
            </Button>
            <Button onClick={() => void batchToggle(true)}>全部启用</Button>
            <Button onClick={() => void batchToggle(false)}>全部禁用</Button>
          </Space>
        }
      />

      <Card size="small">
        <Space wrap size={16}>
          <Input.Search allowClear placeholder="搜索模型 ID / 备注" style={{ width: 240 }} onSearch={setSearch} onChange={(e) => !e.target.value && setSearch('')} />
          <Select allowClear placeholder="平台过滤" style={{ minWidth: 150 }} value={platformFilter} onChange={setPlatformFilter} options={platformOptions} />
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>显示倍率</Typography.Text>
            <Switch size="small" checked={display.show_multiplier} onChange={(v) => void saveDisplay({ show_multiplier: v })} />
          </Space>
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>显示繁忙</Typography.Text>
            <Switch size="small" checked={display.show_busy} onChange={(v) => void saveDisplay({ show_busy: v })} />
          </Space>
          <Space size={8}>
            <Tooltip title="只在对外接口显示官方也可见的模型">
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>官方同显</Typography.Text>
            </Tooltip>
            <Switch size="small" checked={display.official_only} onChange={(v) => void saveDisplay({ official_only: v })} />
          </Space>
        </Space>
      </Card>

      <Card size="small">
        {view === 'flat' ? (
          <Table
            key="view-flat"
            scroll={{ x: 'max-content' }}
            size="small"
            rowKey="id"
            loading={loading}
            dataSource={flatRows}
            pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个` }}
            columns={[
              {
                title: '模型',
                dataIndex: 'model_id',
                ellipsis: true,
                sorter: (a, b) => a.model_id.localeCompare(b.model_id),
                render: (v, m: ModelItem) => (
                  <Space size={4}>
                    <Typography.Text strong>{v}</Typography.Text>
                    <Tooltip title="复制模型名称">
                      <Button
                        type="text"
                        size="small"
                        icon={<CopyOutlined />}
                        style={{ padding: 0, width: 22, height: 22 }}
                        onClick={async (e) => {
                          e.stopPropagation()
                          const ok = await copyText(m.upstream_id || m.model_id)
                          if (ok) message.success(`已复制：${m.upstream_id || m.model_id}`)
                          else message.error('复制失败')
                        }}
                      />
                    </Tooltip>
                    {m.realm && <Tag style={{ fontSize: 11 }}>{m.realm}</Tag>}
                    {m.description && (
                      <Tooltip title={m.description}>
                        <Typography.Text type="secondary">ⓘ</Typography.Text>
                      </Tooltip>
                    )}
                  </Space>
                ),
              },
              { title: '平台', dataIndex: 'platform_label', width: 110 },
              { title: '倍率', width: 90, align: 'right', render: (_, m: ModelItem) => multiplierRender(m) },
              {
                title: '繁忙',
                width: 90,
                render: (_, m: ModelItem) =>
                  display.show_busy && busy[m.model_id] ? (
                    <Tag color={BUSY_TONE[busy[m.model_id].busy_level] ?? 'default'}>{busy[m.model_id].busy_label}</Tag>
                  ) : (
                    FMT_EMPTY
                  ),
              },
              { title: '健康 24h', width: 110, align: 'right', render: (_, m: ModelItem) => healthRender(m.model_id) },
              {
                title: '备注',
                dataIndex: 'note',
                ellipsis: true,
                render: (v) => v || FMT_EMPTY,
              },
              {
                title: '启用',
                dataIndex: 'enabled',
                width: 80,
                render: (v, m: ModelItem) => <Switch size="small" checked={v} onChange={() => void toggleEnabled(m)} />,
              },
              {
                title: '操作',
                width: 140,
                render: (_, m: ModelItem) => (
                  <Space size={0}>
                    <Button type="link" size="small" onClick={() => void speedTest(m)}>
                      测速
                    </Button>
                    <Popconfirm title={`移除 ${m.model_id}？`} onConfirm={() => void removeModel(m)}>
                      <Button type="link" size="small" danger>
                        移除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        ) : (
          <Table
            key="view-aggregate"
            scroll={{ x: 'max-content' }}
            size="small"
            rowKey="key"
            loading={loading}
            dataSource={aggRows}
            pagination={{ pageSize: 20 }}
            columns={[
              {
                title: '模型',
                dataIndex: 'name',
                ellipsis: true,
                render: (v, r: AggItem) => (
                  <Space size={4}>
                    <Typography.Text strong>{v}</Typography.Text>
                    {r.platform_count > 1 && <Tag color="processing">{r.platform_count} 平台</Tag>}
                    {r.free_any && <Tag color="success">含 FREE</Tag>}
                  </Space>
                ),
              },
              { title: '最低倍率', dataIndex: 'min_multiplier', width: 90, align: 'right', render: (v) => (v !== null && v >= 0 ? `×${v}` : FMT_EMPTY) },
              {
                title: '启用',
                dataIndex: 'enabled_any',
                width: 80,
                render: (v) => (v ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>),
              },
            ]}
            expandable={{
              expandedRowRender: (r: AggItem) => (
                <Table
                  size="small"
                  rowKey={(p) => `${p.platform}-${p.model_id}`}
                  dataSource={r.platforms}
                  pagination={false}
                  columns={[
                    { title: '平台', dataIndex: 'platform_label', width: 120 },
                    { title: '平台模型 ID', dataIndex: 'model_id', ellipsis: true },
                    { title: '倍率', dataIndex: 'multiplier', width: 80, align: 'right', render: (v) => (v !== null && v !== undefined && v >= 0 ? `×${v}` : FMT_EMPTY) },
                    { title: '备注', dataIndex: 'note', ellipsis: true, render: (v) => v || FMT_EMPTY },
                    { title: '启用', dataIndex: 'enabled', width: 80, render: (v) => (v ? <Tag color="success">✓</Tag> : <Tag>✗</Tag>) },
                  ]}
                />
              ),
            }}
          />
        )}
      </Card>
    </Space>
  )
}
