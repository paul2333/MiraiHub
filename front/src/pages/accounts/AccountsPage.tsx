import {
  Alert,
  App as AntdApp,
  Avatar,
  Button,
  Card,
  Col,
  Collapse,
  Dropdown,
  Input,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CloudUploadOutlined,
  ExportOutlined,
  LoginOutlined,
  MoreOutlined,
  ReloadOutlined,
  ScanOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import { usePlatform } from '../../context/PlatformContext'
import { useJob } from '../../context/JobContext'
import PageHeader from '../../components/PageHeader'
import PlatformActionsBar from '../../components/PlatformActionsBar'
import DeviceAuthModal from '../../components/DeviceAuthModal'
import PhoneLoginModal from './PhoneLoginModal'
import {
  ConcurrencyModal,
  CreditDetailsModal,
  ExportModal,
  HealthCheckModal,
  ImportModal,
  ModelLimitsModal,
  ReauthModal,
  RequestUsageModal,
} from './AccountModals'
import type { GenericAccount, WbAccount, WbSummary } from './types'
import { isAndroid } from '../../bridges/native'
import { FMT_EMPTY, fmtCredit, fmtEpoch, fmtInt, fmtK, fmtTime, secondsAfter } from '../../utils/format'
import { downloadResponse } from '../../utils/download'

const PS = 20

export default function AccountsPage() {
  const { platforms, current } = usePlatform()
  const spec = platforms.find((p) => p.id === current.platform)
  const storage = (spec?.storage_platform || current.platform).toLowerCase()
  // key 按对外平台 id：trae-cn ↔ trae-intl、qoder ↔ qoder-intl 共用同一
  // storage 实现，不加 key 时组件被复用，realm 等本地状态停在旧区域，
  // 页面内容不更新（切不同产品没事，因为组件类型本来就变了）。
  if (storage === 'workbuddy') return <WorkbuddyAccounts key={spec?.id ?? storage} spec={spec} />
  return <GenericAccounts key={spec?.id ?? storage} spec={spec} storage={storage} />
}

/* ============================ WorkBuddy 账号池 ============================ */

function WorkbuddyAccounts({ spec }: { spec: ReturnType<typeof usePlatform>['platforms'][number] | undefined }) {
  const { message, modal } = AntdApp.useApp()
  const { platformKey, refreshCounts, localFeatures } = usePlatform()
  const { run: runJob } = useJob()
  const [items, setItems] = useState<WbAccount[]>([])
  const [summary, setSummary] = useState<WbSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<'all' | 'active' | 'disabled'>('all')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [phoneOpen, setPhoneOpen] = useState(false)
  const [reauthId, setReauthId] = useState<number | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [limitsOpen, setLimitsOpen] = useState(false)
  const [concAcc, setConcAcc] = useState<WbAccount | null>(null)
  const [creditAcc, setCreditAcc] = useState<WbAccount | null>(null)
  const [usageAcc, setUsageAcc] = useState<WbAccount | null>(null)
  const [healthResult, setHealthResult] = useState<Record<string, unknown> | null>(null)
  const [poolOpen, setPoolOpen] = useState(false)
  const [poolModel, setPoolModel] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<{ items: WbAccount[]; summary: WbSummary }>('/api/accounts?page_size=1000')
      setItems(r.items ?? [])
      setSummary(r.summary ?? null)
    } catch (e) {
      message.error((e as Error).message || '账号池加载失败')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platformKey])

  useEffect(() => {
    void load()
    void refreshCounts()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load])

  // 30s 自动刷新（页面可见时）
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, 30000)
    return () => window.clearInterval(timer)
  }, [load])

  const filtered = useMemo(() => {
    let list = items
    if (filter === 'active') list = list.filter((a) => a.status === 'active')
    if (filter === 'disabled') list = list.filter((a) => a.status !== 'active')
    const q = search.trim().toLowerCase()
    if (q) list = list.filter((a) => a.name.toLowerCase().includes(q) || String(a.uid).toLowerCase().includes(q))
    return list
  }, [items, filter, search])

  const pageItems = filtered.slice((page - 1) * PS, page * PS)

  const toggleAcc = async (a: WbAccount) => {
    try {
      await api(`/api/accounts/${a.id}`, { method: 'PATCH', body: { status: a.status === 'active' ? 'disabled' : 'active' } })
      message.success(a.status === 'active' ? '已停用' : '已启用')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const delAcc = async (a: WbAccount) => {
    try {
      await api(`/api/accounts/${a.id}`, { method: 'DELETE' })
      message.success('已删除')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const refreshAcc = async (a: WbAccount) => {
    try {
      await api(`/api/accounts/${a.id}/refresh`, { method: 'POST', body: {} })
      message.success(`已刷新 ${a.name}`)
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const refreshAll = async () => {
    message.loading({ content: '正在刷新全部账号余额…', key: 'refreshAll', duration: 0 })
    try {
      await api('/api/accounts/credits/sync', { method: 'POST', body: {}, timeout: 0 })
      message.success({ content: '刷新完成', key: 'refreshAll' })
      void load()
    } catch (e) {
      message.error({ content: (e as Error).message || '刷新失败', key: 'refreshAll' })
    }
  }

  // 健康检查（单账号）后端同样返回异步 job 快照，统一走批量任务轮询弹窗
  const healthAcc = async (a: WbAccount) => {
    await runJob({
      title: `健康检查：${a.name}`,
      submitUrl: `/api/accounts/${a.id}/health-check`,
      onFinish: () => void load(),
    })
  }

  const healthAll = async () => {
    runJob({
      title: '一键健康检查',
      submitUrl: '/api/accounts/health-check-batch',
      onFinish: () => void load(),
    })
  }

  const growthBatch = async (ids?: number[]) => {
    runJob({
      title: '成长任务',
      submitUrl: '/api/growth/run-async',
      body: ids?.length ? { account_ids: ids } : {},
      onFinish: () => void load(),
    })
  }

  const healthBatch = async (ids?: number[]) => {
    runJob({
      title: '一键健康检查',
      submitUrl: '/api/accounts/health-check-batch',
      body: ids?.length ? { account_ids: ids } : {},
      onFinish: () => void load(),
    })
  }

  const catTravelBatch = async (ids?: number[]) => {
    runJob({
      title: '猫猫旅行',
      submitUrl: '/api/accounts/cat-travel/batch-async',
      body: ids?.length ? { account_ids: ids } : {},
      onFinish: () => void load(),
    })
  }

  const catTravelOne = async (a: WbAccount) => {
    message.loading({ content: `猫猫旅行执行中：${a.name}…`, key: `ct${a.id}`, duration: 0 })
    try {
      const r = await api<{ steps?: { message?: string; ok?: boolean; reward?: number }[]; credit?: number }>(`/api/accounts/${a.id}/cat-travel`, {
        method: 'POST',
        body: {},
        timeout: 300000,
      })
      message.success({ content: `旅行完成${r.credit ? `，+${fmtCredit(r.credit)} 积分` : ''}`, key: `ct${a.id}` })
      void load()
    } catch (e) {
      message.error({ content: (e as Error).message || '旅行失败', key: `ct${a.id}` })
    }
  }

  const injectAcc = async (a: WbAccount) => {
    modal.confirm({
      title: `注入本机客户端：${a.name}`,
      content: '把该账号登录态写入本机 WorkBuddy 客户端，客户端将被接管使用。',
      okText: '确认注入',
      onOk: async () => {
        try {
          await api(`/api/accounts/${a.id}/inject`, { method: 'POST', body: { confirm: true } })
          message.success('已注入')
        } catch (e) {
          message.error((e as Error).message)
        }
      },
    })
  }

  const exportOne = async (a: WbAccount, format: 'client' | 'backup') => {
    try {
      const resp = await api<Response>(`/api/accounts/${a.id}/export?format=${format}`, { raw: true })
      await downloadResponse(resp, `${a.name}.${format === 'client' ? 'info' : 'json'}`)
      message.success('已下载')
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const scanLocal = async () => {
    try {
      const r = await api<{ found?: number; accounts?: unknown[] }>('/api/accounts/scan-local')
      const found = Array.isArray(r.accounts) ? r.accounts.length : Number(r.found ?? 0)
      modal.confirm({
        title: `扫描到 ${found} 个本机登录态`,
        content: '是否立即导入号池？',
        okText: '导入',
        onOk: async () => {
          const r2 = await api<{ added?: number; updated?: number }>('/api/accounts/import-local', { method: 'POST', body: {} })
          message.success(`导入完成：新增 ${fmtInt(r2.added)}，更新 ${fmtInt(r2.updated)}`)
          void load()
          void refreshCounts()
        },
      })
    } catch (e) {
      message.error((e as Error).message || '扫描失败')
    }
  }

  const clearCooldown = async (a: WbAccount) => {
    try {
      await api(`/api/accounts/${a.id}/cooldown/clear`, { method: 'POST', body: {} })
      message.success('已解除冷却')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const tokenBadge = (a: WbAccount) => {
    if (!a.token_supported) return null
    const tone =
      a.token_state === 'expired' || a.renewal_state === 'expired'
        ? 'error'
        : a.token_state === 'expiring' || a.renewal_state === 'expiring'
          ? 'warning'
          : 'default'
    const label =
      a.token_state === 'expired'
        ? 'Token 已过期'
        : a.token_state === 'expiring'
          ? `Token 剩 ${fmtInt(a.token_days_left)} 天`
          : a.renewal_state === 'expired'
            ? '续期窗口已关闭'
            : a.renewal_state === 'expiring'
              ? `续期剩 ${fmtInt(a.renewal_days_left)} 天`
              : null
    return label ? (
      <Tooltip title={label}>
        <Tag color={tone} style={{ fontSize: 11 }}>
          ⏱
        </Tag>
      </Tooltip>
    ) : null
  }

  const moreMenu = (a: WbAccount) => ({
    items: [
      { key: 'conc', label: '并发上限' },
      { key: 'reauth', label: '重新登录' },
      { key: 'growth', label: '成长任务' },
      { key: 'travel', label: '猫猫旅行' },
      { key: 'credit', label: '积分明细' },
      { key: 'usage', label: '请求用量' },
      ...(isAndroid() ? [] : [{ key: 'inject', label: '注入本机客户端' }]),
      { key: 'export-client', label: '导出登录态' },
      { key: 'export-backup', label: '导出备份' },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === 'conc') setConcAcc(a)
      else if (key === 'reauth') setReauthId(a.id)
      else if (key === 'growth') void growthBatch([a.id])
      else if (key === 'travel') void catTravelOne(a)
      else if (key === 'credit') setCreditAcc(a)
      else if (key === 'usage') setUsageAcc(a)
      else if (key === 'inject') void injectAcc(a)
      else if (key === 'export-client') void exportOne(a, 'client')
      else if (key === 'export-backup') void exportOne(a, 'backup')
    },
  })

  const rowSelection = {
    selectedRowKeys: selectedIds,
    onChange: (keys: React.Key[]) => setSelectedIds(keys as number[]),
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <PageHeader
        title="账号池"
        description={`${fmtInt(summary?.total)} 个账号 · 可用 ${fmtInt(summary?.available)}`}
        onRefresh={() => void load()}
        refreshing={loading}
        actions={
          <Space wrap>
            <Button icon={<LoginOutlined />} onClick={() => setPhoneOpen(true)}>
              手机号登录
            </Button>
            <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
              导入
            </Button>
            {!isAndroid() && localFeatures && (
              <Button icon={<ScanOutlined />} onClick={() => void scanLocal()}>
                扫描本机
              </Button>
            )}
            <Button icon={<ExportOutlined />} onClick={() => setExportOpen(true)}>
              导出
            </Button>
            <Dropdown
              menu={{
                items: [
                  { key: 'health', icon: <SafetyCertificateOutlined />, label: '一键健康检查' },
                  { key: 'limits', icon: <ThunderboltOutlined />, label: '模型额度与冷却' },
                  { key: 'refresh', icon: <ReloadOutlined />, label: '刷新全部额度' },
                  { key: 'growth', icon: <span>🌱</span>, label: '成长任务（全部）' },
                  { key: 'travel', icon: <span>🐈</span>, label: '猫猫旅行（全部）' },
                ],
                onClick: ({ key }) => {
                  if (key === 'health') void healthAll()
                  else if (key === 'limits') setLimitsOpen(true)
                  else if (key === 'refresh') void refreshAll()
                  else if (key === 'growth') void growthBatch()
                  else if (key === 'travel') void catTravelBatch()
                },
              }}
            >
              <Button>管理操作 ▾</Button>
            </Dropdown>
            {/* 平台动作（授权/扫描/签到等）移到页头上方，不再压在表格下面 */}
            {spec && <PlatformActionsBar spec={spec} onChanged={() => void load()} />}
          </Space>
        }
      />

      {/* 概览统计 */}
      <Row gutter={12}>
        <Col xs={12} md={4}><Card size="small"><Statistic title="账号" value={summary?.total ?? 0} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title="启用" value={summary?.active ?? 0} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title="剩余积分" value={fmtCredit(summary?.balance_remain)} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title="7天内到期" value={fmtCredit(summary?.credits_expiring_soon)} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title="Token 临期" value={summary?.token_expiring ?? 0} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title="停用余额" value={fmtCredit(summary?.disabled_balance_remain)} /></Card></Col>
      </Row>

      <Card size="small">
        <Space wrap size={12} style={{ marginBottom: 12 }}>
          <Segmented
            value={filter}
            onChange={(v) => {
              setFilter(v as typeof filter)
              setPage(1)
            }}
            options={[
              { label: `全部 ${fmtInt(summary?.total)}`, value: 'all' },
              { label: `已启用 ${fmtInt(summary?.active)}`, value: 'active' },
              { label: `已停用 ${fmtInt(summary?.disabled)}`, value: 'disabled' },
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜索名称 / UID"
            style={{ width: 220 }}
            onSearch={setSearch}
            onChange={(e) => {
              if (!e.target.value) setSearch('')
            }}
          />
          {selectedIds.length > 0 && (
            <Space>
              <Typography.Text type="secondary">已选 {selectedIds.length}：</Typography.Text>
              <Button size="small" onClick={() => void healthBatch(selectedIds.length ? selectedIds : undefined)}>
                健康检查
              </Button>
              <Button size="small" onClick={() => void growthBatch(selectedIds)}>
                成长任务
              </Button>
              <Button size="small" onClick={() => void catTravelBatch(selectedIds)}>
                猫猫旅行
              </Button>
            </Space>
          )}
        </Space>
        <Table
          scroll={{ x: 'max-content' }}
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={pageItems}
          rowSelection={rowSelection}
          pagination={{
            current: page,
            pageSize: PS,
            total: filtered.length,
            showSizeChanger: false,
            onChange: (p) => setPage(p),
          }}
          columns={[
            {
              title: '账号',
              ellipsis: true,
              render: (_, a: WbAccount) => (
                <Space size={4}>
                  <Avatar size={22} style={{ backgroundColor: '#2E9BFF', fontSize: 12 }}>{a.name.slice(0, 1)}</Avatar>
                  <Typography.Text strong>{a.name}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>{a.uid}</Typography.Text>
                  {tokenBadge(a)}
                  {a.gateway_state === 'cooling' && (
                    <Tooltip title={`${a.gateway_reason || '冷却中'}${a.gateway_until ? ` · ${secondsAfter(new Date(a.gateway_until).getTime()) ?? 0}s 后恢复` : ''}（点击解除）`}>
                      <Tag color="warning" style={{ fontSize: 11, cursor: 'pointer' }} onClick={() => void clearCooldown(a)}>
                        冷却
                      </Tag>
                    </Tooltip>
                  )}
                  {a.model_limits?.length > 0 && (
                    <Tooltip title={a.model_limits.map((m) => `${m.model}：${Math.ceil(m.remaining_seconds / 60)} 分钟`).join('，')}>
                      <Tag color="warning" style={{ fontSize: 11, cursor: 'pointer' }} onClick={() => setLimitsOpen(true)}>
                        模型限额 {a.model_limits.length}
                      </Tag>
                    </Tooltip>
                  )}
                </Space>
              ),
            },
            { title: '状态', dataIndex: 'status', width: 80, render: (v) => (v === 'active' ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>) },
            {
              title: '积分',
              width: 130,
              align: 'right',
              render: (_, a: WbAccount) => <span className="num">{fmtCredit(a.balance_remain)} / {fmtCredit(a.balance_total)}</span>,
            },
            {
              title: '到期',
              width: 120,
              render: (_, a: WbAccount) => {
                const days = a.credits_soonest_days_left
                if (days === null || days === undefined) return FMT_EMPTY
                if (days <= 0) return <Tag color="error">已到期</Tag>
                if (days <= 7) return <Tag color="warning">{Math.ceil(days)} 天</Tag>
                return <span className="num">{Math.ceil(days)} 天</span>
              },
            },
            { title: '余额同步', dataIndex: 'last_sync_at', width: 130, render: (v) => <span className="num">{fmtTime(v)}</span> },
            {
              title: '操作',
              width: 280,
              render: (_, a: WbAccount) => (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => void healthAcc(a)}>
                    体检
                  </Button>
                  <Button type="link" size="small" onClick={() => void refreshAcc(a)}>
                    刷新
                  </Button>
                  <Button type="link" size="small" onClick={() => void toggleAcc(a)}>
                    {a.status === 'active' ? '停用' : '启用'}
                  </Button>
                  <Popconfirm title={`确认删除 ${a.name}？`} onConfirm={() => void delAcc(a)}>
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                  <Dropdown menu={moreMenu(a)} trigger={['click']}>
                    <Button type="text" size="small" icon={<MoreOutlined />} />
                  </Dropdown>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      {/* 模型使用统计 */}
      <Collapse
        items={[
          {
            key: 'pool',
            label: '模型使用统计（号池视角）',
            extra: (
              <Button
                size="small"
                type="text"
                onClick={(e) => {
                  e.stopPropagation()
                  setPoolOpen(true)
                }}
              >
                详情
              </Button>
            ),
            children: (
              <Typography.Text type="secondary">展开详情查看各模型今日消耗与可用账号预估。</Typography.Text>
            ),
          },
        ]}
      />

      {/* 弹窗群 */}
      <PhoneLoginModal open={phoneOpen} onClose={() => setPhoneOpen(false)} onSuccess={() => void load()} />
      <ReauthModal account={items.find((a) => a.id === reauthId) ?? null} onClose={() => setReauthId(null)} onDone={() => void load()} />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={() => void load()} />
      <ExportModal open={exportOpen} onClose={() => setExportOpen(false)} selectedIds={selectedIds} />
      <ModelLimitsModal open={limitsOpen} onClose={() => setLimitsOpen(false)} onDone={() => void load()} />
      <ConcurrencyModal account={concAcc} onClose={() => setConcAcc(null)} onDone={() => void load()} />
      <CreditDetailsModal account={creditAcc} onClose={() => setCreditAcc(null)} />
      <RequestUsageModal account={usageAcc} onClose={() => setUsageAcc(null)} />
      <HealthCheckModal result={healthResult} onClose={() => setHealthResult(null)} />
      <ModelPoolModal open={poolOpen} model={poolModel} onModelChange={setPoolModel} onClose={() => setPoolOpen(false)} />
    </Space>
  )
}

/** 模型使用统计（GET /api/stats/model-pool） */
function ModelPoolModal({
  open,
  model,
  onModelChange,
  onClose,
}: {
  open: boolean
  model: string
  onModelChange: (m: string) => void
  onClose: () => void
}) {
  const [models, setModels] = useState<{ model: string; enabled: boolean; requests_today: number; tokens_today: number; exhausted_accounts: number }[]>([])
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    api<{ models?: typeof models }>(model ? `/api/stats/model-pool?model=${encodeURIComponent(model)}` : '/api/stats/model-pool')
      .then((r) => {
        if (model) setDetail(r as Record<string, unknown>)
        else setModels((r.models ?? []) as typeof models)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [open, model])

  const accounts = (detail?.accounts ?? []) as Record<string, unknown>[]
  const summary = (detail?.summary ?? {}) as Record<string, number | string | null>

  return (
    <Modal title="模型使用统计" open={open} onCancel={onClose} footer={null} width={820}>
      <Select
        showSearch={{ optionFilterProp: 'label' }}
        allowClear
        placeholder="选择模型查看明细"
        style={{ width: '100%', marginBottom: 12 }}
        value={model || undefined}
        onChange={(v) => onModelChange(v ?? '')}
        options={models.map((m) => ({ value: m.model, label: `${m.model}（今日 ${m.requests_today} 次 / ${fmtK(m.tokens_today)} tok）` }))}
      />
      {loading && (
        <div style={{ textAlign: 'center', padding: 24 }}>
          <Spin />
        </div>
      )}
      {model && summary && (
        <>
          <Space wrap size={16} style={{ marginBottom: 12 }}>
            <Statistic title="可用账号" value={summary.usable_accounts ?? 0} />
            <Statistic title="今日额度耗尽" value={summary.exhausted_accounts ?? 0} />
            <Statistic title="冷却中" value={summary.cooling_accounts ?? 0} />
            <Statistic title="今日请求" value={summary.requests_today ?? 0} />
            <Statistic title="24h Tokens" value={fmtK(Number(summary.rolling_24h ? (summary.rolling_24h as { tokens?: number }).tokens ?? 0 : 0))} />
          </Space>
          <Table
            scroll={{ x: 'max-content' }}
            size="small"
            rowKey={(r) => String(r.id)}
            loading={loading}
            dataSource={accounts}
            pagination={{ pageSize: 10 }}
            columns={[
              { title: '账号', dataIndex: 'name', ellipsis: true },
              { title: '状态', dataIndex: 'usable', width: 90, render: (v: boolean, r) => (r.cooling ? <Tag color="warning">冷却</Tag> : v ? <Tag color="success">可用</Tag> : <Tag>受限</Tag>) },
              { title: '今日请求', dataIndex: 'requests_today', width: 90, align: 'right', render: (v) => <span className="num">{fmtInt(v)}</span> },
              { title: '今日 Token', dataIndex: 'used_today_tokens', width: 100, align: 'right', render: (v) => <span className="num">{fmtK(v)}</span> },
              { title: '余额', dataIndex: 'balance_remain', width: 90, align: 'right', render: (v) => <span className="num">{fmtCredit(v)}</span> },
            ]}
          />
        </>
      )}
    </Modal>
  )
}

/* ============================ 通用平台（Trae/Qoder/Loomy） ============================ */

function GenericAccounts({ spec, storage }: { spec: ReturnType<typeof usePlatform>['platforms'][number] | undefined; storage: string }) {
  const { message } = AntdApp.useApp()
  const { current, refreshCounts, localFeatures } = usePlatform()
  const [items, setItems] = useState<GenericAccount[]>([])
  const [summary, setSummary] = useState<Record<string, number> | null>(null)
  const [loading, setLoading] = useState(false)
  const [realm, setRealm] = useState<string>(current.realm || '')
  const [authOpen, setAuthOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importLocalOpen, setImportLocalOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [testResult, setTestResult] = useState<{ name: string; realm: string; stages: Record<string, Record<string, unknown>> } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const qs = realm ? `?realm=${encodeURIComponent(realm)}` : ''
      const r = await api<{ items: GenericAccount[]; summary?: Record<string, number> }>(`/api/${storage}/accounts${qs}`)
      setItems(r.items ?? [])
      setSummary(r.summary ?? null)
    } catch (e) {
      message.error((e as Error).message || '账号加载失败')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storage, realm])

  useEffect(() => {
    void load()
    void refreshCounts()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load])

  // 30s 自动刷新（页面可见时）：Loomy 等平台的配额由后端后台线程刷新，靠它带出新值
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, 30000)
    return () => window.clearInterval(timer)
  }, [load])

  const refreshOne = async (a: GenericAccount, force = false) => {
    try {
      await api(`/api/${storage}/accounts/${a.id}/refresh${force ? '?force=true' : ''}`, { method: 'POST', body: {} })
      message.success(`已刷新 ${a.name}`)
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const testOne = async (a: GenericAccount) => {
    message.loading({ content: `正在检测 ${a.name}…`, key: `t${a.id}`, duration: 0 })
    try {
      const r = await api<Record<string, unknown>>(`/api/${storage}/accounts/${a.id}/test`, { method: 'POST', body: {}, timeout: 180000 })
      message.success({ content: '检测完成', key: `t${a.id}` })
      // 响应形如 { realm, stages: { token/models/conversation/quota } }
      const o = r as { realm?: string; stages?: Record<string, unknown> }
      setTestResult({ name: a.name, realm: o.realm ?? '', stages: (o.stages ?? o) as Record<string, Record<string, unknown>> })
    } catch (e) {
      message.error({ content: (e as Error).message || '检测失败', key: `t${a.id}` })
    }
  }

  const toggleOne = async (a: GenericAccount) => {
    try {
      await api(`/api/${storage}/accounts/${a.id}`, {
        method: 'PATCH',
        body: { status: a.status === 'active' ? 'disabled' : 'active' },
      })
      message.success(a.status === 'active' ? '已停用' : '已启用')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const delOne = async (a: GenericAccount) => {
    try {
      await api(`/api/${storage}/accounts/${a.id}`, { method: 'DELETE' })
      message.success('已删除')
      void load()
      void refreshCounts()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const refreshAll = async () => {
    message.loading({ content: '批量刷新中…', key: 'gRefreshAll', duration: 0 })
    try {
      const r = await api<{ ok?: number; failed?: number }>(`/api/${storage}/refresh-all${realm ? `?realm=${encodeURIComponent(realm)}` : ''}`, { method: 'POST', body: {}, timeout: 0 })
      message.success({ content: `刷新完成：成功 ${r.ok ?? 0}，失败 ${r.failed ?? 0}`, key: 'gRefreshAll' })
      void load()
    } catch (e) {
      message.error({ content: (e as Error).message || '刷新失败', key: 'gRefreshAll' })
    }
  }

  const realmTabs = spec?.realms?.length
    ? [
        { label: '全部', value: '' },
        ...spec.realms.map((r) => ({ label: r.label, value: r.key })),
      ]
    : null

  const balanceCol = {
    title: '额度',
    width: 150,
    render: (_v: unknown, a: GenericAccount) => {
      if (storage === 'qoder') {
        return (
          <span className="num">
            {fmtCredit(a.balance_remain)} / {fmtCredit(a.balance_total)}
            {a.addon_total ? ` +${fmtCredit(a.addon_remain)}` : ''}
          </span>
        )
      }
      if (storage === 'loomy') {
        return (
          <span className="num">
            {fmtCredit(a.balance_remain)} · 今日 {fmtCredit(a.daily_balance)}
          </span>
        )
      }
      return (
        <span className="num">
          {fmtCredit(a.balance_remain)} / {fmtCredit(a.balance_total)} {a.balance_unit || ''}
        </span>
      )
    },
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <PageHeader
        title={`${spec?.label ?? storage} 账号池`}
        description={summary ? `共 ${fmtInt(summary.total)} · 启用 ${fmtInt(summary.active)} · 停用 ${fmtInt(summary.disabled)}` : undefined}
        onRefresh={() => void load()}
        refreshing={loading}
        actions={
          <Space wrap>
            {(spec?.actions ?? []).includes('begin-auth') && (
              <Button type="primary" icon={<LoginOutlined />} onClick={() => setAuthOpen(true)}>
                授权接入
              </Button>
            )}
            <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
              导入
            </Button>
            {(spec?.actions ?? []).includes('import') && localFeatures && (
              <Button icon={<CloudUploadOutlined />} onClick={() => setImportLocalOpen(true)}>
                导入本机
              </Button>
            )}
            <Button icon={<ExportOutlined />} onClick={() => setExportOpen(true)}>
              导出
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void refreshAll()}>
              刷新全部额度
            </Button>
            {/* 平台动作（授权/扫描/签到等）移到页头上方，不再压在表格下面 */}
            {spec && <PlatformActionsBar spec={spec} onChanged={() => void load()} />}
          </Space>
        }
      />

      <Card size="small">
        <Space wrap size={12} style={{ marginBottom: 12 }}>
          {realmTabs && (
            <Segmented value={realm} onChange={(v) => setRealm(v as string)} options={realmTabs} />
          )}
        </Space>
        <Table
          scroll={{ x: 'max-content' }}
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个` }}
          columns={[
            {
              title: '账号',
              ellipsis: true,
              render: (_, a) => (
                <Space size={4}>
                  <Typography.Text strong>{a.name}</Typography.Text>
                  {a.email && <Typography.Text type="secondary" style={{ fontSize: 11 }}>{a.email}</Typography.Text>}
                  {a.realm && <Tag style={{ fontSize: 11 }}>{a.realm}</Tag>}
                  {a.cool_until && (
                    <Tooltip title={a.last_err_msg || '冷却中'}>
                      <Tag color="warning" style={{ fontSize: 11 }}>冷却</Tag>
                    </Tooltip>
                  )}
                </Space>
              ),
            },
            ...(storage === 'qoder'
              ? [{ title: '套餐', dataIndex: 'plan', width: 110, render: (v: string) => v || FMT_EMPTY }]
              : storage === 'loomy'
                ? [
                    { title: '套餐', dataIndex: 'plan', width: 110, render: (v: string) => v || '计量制' },
                    { title: '模型数', dataIndex: 'models', width: 80, align: 'right' as const, render: (v?: number) => <span className="num">{fmtInt(v ?? null)}</span> },
                  ]
                : [{ title: '套餐/配额', width: 150, render: (_v: unknown, a: GenericAccount) => <span className="num">{a.quota?.plan || a.quota?.unit || FMT_EMPTY}{a.quota?.total ? ` · ${fmtCredit(a.quota.total)}` : ''}</span> }]),
            balanceCol,
            { title: '状态', dataIndex: 'status', width: 80, render: (v) => (v === 'active' ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>) },
            { title: '同步', dataIndex: 'last_sync_at', width: 130, render: (v) => <span className="num">{fmtTime(v ?? null)}</span> },
            {
              title: '操作',
              width: 250,
              render: (_, a) => (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => void testOne(a)}>
                    检测
                  </Button>
                  <Button type="link" size="small" onClick={() => void refreshOne(a, true)}>
                    刷新
                  </Button>
                  <Button type="link" size="small" onClick={() => void toggleOne(a)}>
                    {a.status === 'active' ? '停用' : '启用'}
                  </Button>
                  <Popconfirm title={`确认删除 ${a.name}？`} onConfirm={() => void delOne(a)}>
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <DeviceAuthModal
        open={authOpen}
        storage={storage}
        realm={realm || current.realm || undefined}
        onClose={() => setAuthOpen(false)}
        onDone={() => void load()}
      />
      <ImportModal open={importOpen} storage={storage} realm={realm || undefined} onClose={() => setImportOpen(false)} onDone={() => { void load(); void refreshCounts() }} />
      <ExportModal open={exportOpen} storage={storage} realm={realm || undefined} onClose={() => setExportOpen(false)} />
      <ImportLocalModal open={importLocalOpen} storage={storage} onClose={() => setImportLocalOpen(false)} onDone={() => { void load(); void refreshCounts() }} />
      <TestResultModal result={testResult} onClose={() => setTestResult(null)} />
    </Space>
  )
}

/** 本机导入弹窗（trae/qoder/loomy 的 import-local 通常是全量导入） */
function ImportLocalModal({ open, storage, onClose, onDone }: { open: boolean; storage: string; onClose: () => void; onDone?: () => void }) {
  const { message } = AntdApp.useApp()
  const [busy, setBusy] = useState(false)

  const doImport = async () => {
    setBusy(true)
    try {
      const r = await api<{ added?: number; updated?: number; errors?: unknown[] }>(`/api/${storage}/import-local`, { method: 'POST', body: {} })
      message.success(`导入完成：新增 ${fmtInt(r.added)}，更新 ${fmtInt(r.updated)}`)
      onDone?.()
      onClose()
    } catch (e) {
      message.error((e as Error).message || '导入失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="导入本机凭证"
      open={open}
      onCancel={onClose}
      onOk={() => void doImport()}
      okText="开始导入"
      confirmLoading={busy}
      width={480}
    >
      <Typography.Text>
        将读取本机 {storage.toUpperCase()} 客户端登录态并导入号池（先"扫描本机"可预览）。
      </Typography.Text>
    </Modal>
  )
}

/* ============================ 检测结果（阶段化人话展示） ============================ */

const STAGE_LABELS: Record<string, string> = {
  token: '令牌校验',
  models: '模型目录',
  conversation: '对话测试',
  quota: '额度查询',
  profile: '账号信息',
  health: '健康检查',
  usage: '用量接口',
}

/** 把单个阶段的返回翻译成用户能读的一句话；说不出细节时给「通过/失败」 */
function stageDetail(key: string, v: Record<string, unknown>): string {
  const parts: string[] = []
  if (key === 'token' && v.expires_at) {
    const ms = Number(v.expires_at) * 1000
    if (Number.isFinite(ms) && ms > 0) parts.push(`令牌到期 ${fmtEpoch(Number(v.expires_at))}`)
  }
  if (key === 'quota' && v.ok) {
    const num = (x: unknown) => (x === null || x === undefined || x === '' ? '—' : String(x))
    parts.push(`剩余 ${num(v.remain)} / ${num(v.total)}${v.plan ? `（套餐 ${v.plan}）` : ''}`)
  }
  if (typeof v.status === 'number' && !v.ok) parts.push(`HTTP ${v.status}`)
  if (v.code !== undefined && v.code !== null && !v.ok) parts.push(`错误码 ${String(v.code)}`)
  const msg = (v.message || v.error || v.detail || v.summary) as string | undefined
  if (typeof msg === 'string' && msg) parts.push(msg)
  if (!parts.length) {
    // 兜底：挑基本类型字段拼一句（不甩 JSON 给用户）
    const extra = Object.entries(v)
      .filter(([k, val]) => !['ok', 'id', 'name'].includes(k) && val !== null && val !== undefined && typeof val !== 'object')
      .map(([k, val]) => `${k} ${String(val)}`)
    if (extra.length) parts.push(extra.join('，'))
  }
  return parts.join('；') || (v.ok ? '通过' : '失败')
}

function TestResultModal({
  result,
  onClose,
}: {
  result: { name: string; realm: string; stages: Record<string, Record<string, unknown>> } | null
  onClose: () => void
}) {
  const stages = Object.entries(result?.stages ?? {}).filter(
    ([k]) => !['id', 'name', 'ok', 'error', 'realm'].includes(k),
  )
  const failed = stages.filter(([, v]) => !v?.ok).length
  const summary = stages.length
    ? failed
      ? { type: 'error' as const, text: `共 ${stages.length} 项检查，${failed} 项未通过` }
      : { type: 'success' as const, text: `全部通过（${stages.length} 项检查）` }
    : null
  return (
    <Modal title={`检测结果：${result?.name ?? ''}`} open={!!result} onCancel={onClose} footer={null} width={640}>
      {result?.realm && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
          区域：{result.realm}
        </Typography.Text>
      )}
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        {summary && <Alert type={summary.type} showIcon message={summary.text} />}
        {stages.map(([k, v]) => {
          const ok = !!v?.ok
          return (
            <Alert
              key={k}
              type={ok ? 'success' : 'error'}
              showIcon
              message={
                <Space size={6}>
                  <span>{STAGE_LABELS[k] ?? k}</span>
                  <Tag color={ok ? 'success' : 'error'} style={{ marginInlineEnd: 0 }}>
                    {ok ? '通过' : '未通过'}
                  </Tag>
                </Space>
              }
              description={stageDetail(k, v ?? {})}
            />
          )
        })}
      </Space>
    </Modal>
  )
}
