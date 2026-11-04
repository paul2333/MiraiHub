import { App as AntdApp, Alert, Button, Card, Checkbox, Col, Collapse, Form, Input, InputNumber, Modal, Popconfirm, Progress, Row, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { EyeOutlined, PlusOutlined } from '@ant-design/icons'
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { usePlatform } from '../context/PlatformContext'
import PageHeader from '../components/PageHeader'
import CopyButton from '../components/CopyButton'
import { FMT_EMPTY, fmtCredit, fmtCreditExact, fmtInt, fmtTime } from '../utils/format'

interface KeyItem {
  id: number
  name: string
  masked: string
  credit_limit: number
  credit_used: number
  unlimited: boolean
  max_concurrent: number | null
  rate_per_minute: number | null
  status: 'active' | 'revoked'
  note: string
  group_id: number
  group_name: string
  platform?: string
  account_ids?: number[]
  account_name?: string
  aggregate?: boolean
  created_at: string
  ip_summary?: {
    unique_ips: number
    requests: number
    active_requests?: number
    top_ips?: { ip: string; requests: number; active_requests?: number }[]
  }
}

interface KeyFormValues {
  name: string
  unlimited?: boolean
  credit_limit?: number
  max_concurrent?: number | null
  rate_per_minute?: number | null
  group_id?: number
  platform?: string
  account_ids?: number[]
  aggregate?: boolean
  note?: string
}

const CURL_EXAMPLE = (base: string, model: string) => `curl ${base}/chat/completions \\
  -H "Authorization: Bearer sk-你的Key" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${model}", "messages": [{"role": "user", "content": "你好"}]}'`

const CLAUDE_EXAMPLE = (base: string, model: string) => `curl ${base}/messages \\
  -H "Authorization: Bearer sk-你的Key" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${model}", "max_tokens": 1024, "messages": [{"role": "user", "content": "你好"}]}'`

export default function KeysPage() {
  const { message } = AntdApp.useApp()
  const { platformKey } = usePlatform()
  const [items, setItems] = useState<KeyItem[]>([])
  const [groups, setGroups] = useState<{ id: number; name: string }[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<KeyItem | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [form] = Form.useForm<KeyFormValues>()
  const [accountOptions, setAccountOptions] = useState<{ id: number; name: string }[]>([])
  const [createdKey, setCreatedKey] = useState<{ name: string; key: string } | null>(null)
  const [usageView, setUsageView] = useState<{ name: string; limit: number; used: number; remaining: number; percent: number | null } | null>(null)
  const [fullKey, setFullKey] = useState<string | null>(null)
  const [ipsFor, setIpsFor] = useState<KeyItem | null>(null)
  const [network, setNetwork] = useState<{ lan_ips: string[]; port: number } | null>(null)
  const [enabledModels, setEnabledModels] = useState<string[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [k, g] = await Promise.all([api<{ items: KeyItem[] }>('/api/keys'), api<{ items: { id: number; name: string }[] }>('/api/groups')])
      setItems(k.items ?? [])
      setGroups(g.items ?? [])
    } catch (e) {
      message.error((e as Error).message || 'Key 列表加载失败')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platformKey])

  useEffect(() => {
    void load()
    void api<{ lan_ips: string[]; port: number }>('/api/app/network').then(setNetwork).catch(() => {})
    void api<{ items: { key: string; enabled_any: boolean; name: string }[] }>('/api/models/aggregate')
      .then((r) => setEnabledModels((r.items ?? []).filter((m) => m.enabled_any).map((m) => m.name).slice(0, 40)))
      .catch(() => {})
  }, [load])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setFormOpen(true)
  }

  const openEdit = (k: KeyItem) => {
    setEditing(k)
    form.setFieldsValue({
      name: k.name,
      credit_limit: k.unlimited ? undefined : k.credit_limit,
      max_concurrent: k.max_concurrent,
      rate_per_minute: k.rate_per_minute,
      group_id: k.group_id || undefined,
      platform: k.platform || undefined,
      account_ids: k.account_ids ?? [],
      aggregate: k.aggregate,
      note: k.note,
    })
    setFormOpen(true)
  }

  const loadAccounts = async (platform?: string) => {
    if (!platform) {
      setAccountOptions([])
      return
    }
    try {
      if (platform === 'workbuddy') {
        const r = await api<{ items: { id: number; name: string }[] }>('/api/accounts?page_size=500')
        setAccountOptions((r.items ?? []).map((a) => ({ id: a.id, name: a.name })))
      } else {
        const r = await api<{ items: { id: number; name: string }[] }>(`/api/${platform}/accounts?page_size=500`)
        setAccountOptions((r.items ?? []).map((a) => ({ id: a.id, name: a.name })))
      }
    } catch {
      setAccountOptions([])
    }
  }

  const submitForm = async () => {
    const values = await form.validateFields()
    // 限额语义：留空或 0 = 不限额度（创建时不传 credit_limit 字段；编辑传 null）
    const limitRaw = values.credit_limit
    const hasLimit = limitRaw !== undefined && limitRaw !== null && Number(limitRaw) > 0
    const body: Record<string, unknown> = {
      name: values.name,
      unlimited: !hasLimit,
      ...(hasLimit ? { credit_limit: Number(limitRaw) } : editing ? { credit_limit: null } : {}),
      max_concurrent: values.max_concurrent ?? null,
      rate_per_minute: values.rate_per_minute ?? null,
      group_id: values.group_id ?? 0,
      platform: values.platform ?? null,
      account_ids: values.account_ids?.length ? values.account_ids : null,
      aggregate: !!values.aggregate,
      note: values.note ?? '',
    }
    try {
      if (editing) {
        await api(`/api/keys/${editing.id}`, { method: 'PATCH', body })
        message.success('Key 已更新')
      } else {
        const r = await api<{ key: string; name: string }>('/api/keys', { method: 'POST', body })
        setCreatedKey({ name: r.name, key: r.key })
      }
      setFormOpen(false)
      void load()
    } catch (e) {
      message.error((e as Error).message || '保存失败')
    }
  }

  const toggleStatus = async (k: KeyItem) => {
    try {
      await api(`/api/keys/${k.id}`, { method: 'PATCH', body: { status: k.status === 'active' ? 'revoked' : 'active' } })
      message.success(k.status === 'active' ? '已停用' : '已启用')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const removeKey = async (k: KeyItem) => {
    try {
      await api(`/api/keys/${k.id}`, { method: 'DELETE' })
      message.success('已删除')
      void load()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const showFull = async (k: KeyItem) => {
    try {
      const r = await api<{ key?: string }>(`/api/keys/${k.id}/view`)
      setFullKey(r.key ?? FMT_EMPTY)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const showUsage = async (k: KeyItem) => {
    try {
      const r = await api<{ credit_limit: number; credit_used: number; credit_remaining: number; usage_percent: number | null; unlimited: boolean }>(`/api/keys/${k.id}/usage`)
      setUsageView({ name: k.name, limit: r.credit_limit, used: r.credit_used, remaining: r.credit_remaining, percent: r.usage_percent })
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const origin = window.location.origin
  const lanAddr = network?.lan_ips?.[0] ? `http://${network.lan_ips[0]}:${network.port}` : ''

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <PageHeader
        title="API Keys"
        onRefresh={() => void load()}
        refreshing={loading}
        actions={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            创建 Key
          </Button>
        }
      />

      <Collapse
        items={[
          {
            key: 'share',
            label: '对外对接信息',
            children: (
              <Space orientation="vertical" size={12} style={{ width: '100%' }}>
                <Row gutter={[12, 12]}>
                  <Col xs={24} md={12}>
                    <Card size="small" title="OpenAI 兼容">
                      <Typography.Text code>{origin}/v1</Typography.Text>
                      <CopyButton value={`${origin}/v1`} tip="复制 Base URL" />
                    </Card>
                  </Col>
                  <Col xs={24} md={12}>
                    <Card size="small" title="Claude 兼容">
                      <Typography.Text code>{origin}</Typography.Text>
                      <CopyButton value={origin} tip="复制 Base URL" />
                    </Card>
                  </Col>
                </Row>
                {lanAddr && (
                  <Typography.Text type="secondary">
                    局域网地址：<Typography.Text code>{lanAddr}/v1</Typography.Text>
                    <CopyButton value={`${lanAddr}/v1`} tip="复制局域网地址" />
                  </Typography.Text>
                )}
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>鉴权：请求头 Authorization: Bearer sk-…（或 X-API-Key）</Typography.Text>
                </div>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>已启用模型：</Typography.Text>
                  <Space wrap size={4} style={{ marginTop: 4 }}>
                    {enabledModels.map((m) => (
                      <Tag key={m}>{m}</Tag>
                    ))}
                    {!enabledModels.length && <Typography.Text type="secondary">（模型配置页启用后在此显示）</Typography.Text>}
                  </Space>
                </div>
                <Card size="small" title="OpenAI / Chat 示例">
                  <Typography.Paragraph code style={{ whiteSpace: 'pre-wrap', marginBottom: 0, fontSize: 12 }}>
                    {CURL_EXAMPLE(`${origin}/v1`, enabledModels[0] ?? 'gpt-4o-mini')}
                  </Typography.Paragraph>
                </Card>
                <Card size="small" title="Claude / Messages 示例">
                  <Typography.Paragraph code style={{ whiteSpace: 'pre-wrap', marginBottom: 0, fontSize: 12 }}>
                    {CLAUDE_EXAMPLE(origin, enabledModels[0] ?? 'claude-3-5-sonnet')}
                  </Typography.Paragraph>
                </Card>
              </Space>
            ),
          },
        ]}
      />

      <Card size="small">
        <Table
          scroll={{ x: 'max-content' }}
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个` }}
          columns={[
            { title: '名称', dataIndex: 'name', ellipsis: true, render: (v, r) => (
              <Space size={4}>
                <Typography.Text strong>{v}</Typography.Text>
                {r.status !== 'active' && <Tag color="error">已停用</Tag>}
                {r.aggregate && <Tag color="processing">聚合</Tag>}
              </Space>
            ) },
            {
              title: 'Key',
              dataIndex: 'masked',
              width: 200,
              render: (v: string) => (
                <Space size={0}>
                  <Typography.Text code style={{ fontSize: 12 }}>{v}</Typography.Text>
                  <Tooltip title="查看完整 Key">
                    <Button
                      type="text"
                      size="small"
                      icon={<EyeOutlined />}
                      onClick={() => void showFull(items.find((i) => i.masked === v)!)}
                    />
                  </Tooltip>
                </Space>
              ),
            },
            {
              title: '限额 / 已用',
              width: 130,
              render: (_, r) =>
                r.unlimited ? (
                  <Tag color="processing">不限额</Tag>
                ) : (
                  <Tooltip title={`已用 ${fmtCreditExact(r.credit_used)} / ${fmtCredit(r.credit_limit)}`}>
                    <span className="num">
                      {fmtCreditExact(r.credit_used)} / {fmtCredit(r.credit_limit)}
                    </span>
                  </Tooltip>
                ),
            },
            { title: '并发 / 频率', width: 120, render: (_, r) => <span className="num">{r.max_concurrent ?? '全局'} / {r.rate_per_minute ?? '不限'}</span> },
            {
              title: '来源 IP',
              width: 150,
              render: (_, r) =>
                r.ip_summary ? (
                  <Space size={4} wrap>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }} className="num">
                      {fmtInt(r.ip_summary.unique_ips)} IP
                      {r.ip_summary.active_requests ? ` · ${fmtInt(r.ip_summary.active_requests)} 活跃` : ''}
                    </Typography.Text>
                    {(r.ip_summary.top_ips ?? []).slice(0, 3).map((t) => (
                      <Tooltip key={t.ip} title={t.ip}>
                        <Tag style={{ fontSize: 11 }} onClick={() => setIpsFor(r)}>{t.ip}</Tag>
                      </Tooltip>
                    ))}
                    <Button type="link" size="small" onClick={() => setIpsFor(r)}>
                      详情
                    </Button>
                  </Space>
                ) : (
                  FMT_EMPTY
                ),
            },
            { title: '分组', dataIndex: 'group_name', width: 110, render: (v) => v || FMT_EMPTY },
            { title: '创建时间', dataIndex: 'created_at', width: 130, render: (v) => <span className="num">{fmtTime(v)}</span> },
            {
              title: '操作',
              width: 240,
              render: (_, r) => (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => void showUsage(r)}>
                    用量
                  </Button>
                  <Button type="link" size="small" onClick={() => openEdit(r)}>
                    调整
                  </Button>
                  <Button type="link" size="small" onClick={() => void toggleStatus(r)}>
                    {r.status === 'active' ? '停用' : '启用'}
                  </Button>
                  <Popconfirm title="确认删除该 Key？" onConfirm={() => void removeKey(r)}>
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

      {/* 创建/编辑弹窗 */}
      <Modal
        title={editing ? `调整 Key：${editing.name}` : '创建 API Key'}
        open={formOpen}
        onCancel={() => setFormOpen(false)}
        onOk={() => void submitForm()}
        okText={editing ? '保存' : '创建'}
        width={620}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：claude-code 主力 Key" />
          </Form.Item>
          <Form.Item name="credit_limit" label="积分限额（选填）" extra="留空或填 0 = 不限额度">
            <InputNumber style={{ width: '100%' }} min={0} placeholder="不限额度" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="max_concurrent" label="并发上限（空 = 全局）">
                <InputNumber style={{ width: '100%' }} min={0} placeholder="全局" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="rate_per_minute" label="每分钟请求数（空 = 不限）">
                <InputNumber style={{ width: '100%' }} min={0} placeholder="不限" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="group_id" label="模型分组">
            <Select allowClear placeholder="不限制" options={groups.map((g) => ({ value: g.id, label: g.name }))} />
          </Form.Item>
          <Form.Item name="platform" label="绑定平台" initialValue={undefined}>
            <Select
              allowClear
              placeholder="不绑定（默认走调度）"
              onChange={(v) => void loadAccounts(v)}
              options={[
                { value: 'workbuddy', label: 'WorkBuddy' },
                { value: 'trae', label: 'Trae' },
                { value: 'qoder', label: 'Qoder' },
                { value: 'loomy', label: 'Loomy' },
              ]}
            />
          </Form.Item>
          <Form.Item name="account_ids" label="独占绑定账号">
            <Select mode="multiple" allowClear maxTagCount={4} placeholder="可选" options={accountOptions.map((a) => ({ value: a.id, label: a.name }))} />
          </Form.Item>
          <Form.Item name="aggregate" valuePropName="checked" initialValue={false}>
            <Checkbox>聚合模式（跨平台聚合账号池）</Checkbox>
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 新 Key 展示 */}
      <Modal
        title="Key 创建成功"
        open={!!createdKey}
        onCancel={() => setCreatedKey(null)}
        footer={[<Button key="ok" type="primary" onClick={() => setCreatedKey(null)}>我已保存</Button>]}
      >
        <Alert type="warning" showIcon title="完整 Key 仅此一次展示，请立即复制保存。" style={{ marginBottom: 12 }} />
        <Typography.Paragraph code copyable style={{ wordBreak: 'break-all', marginBottom: 0 }}>
          {createdKey?.key}
        </Typography.Paragraph>
      </Modal>

      {/* 明文 Key */}
      <Modal title="完整 Key" open={!!fullKey} onCancel={() => setFullKey(null)} footer={null} width={560}>
        <Typography.Paragraph code copyable style={{ wordBreak: 'break-all', marginBottom: 0 }}>
          {fullKey}
        </Typography.Paragraph>
      </Modal>

      {/* 用量弹窗 */}
      <Modal title={`用量：${usageView?.name ?? ''}`} open={!!usageView} onCancel={() => setUsageView(null)} footer={null} width={480}>
        {usageView && (
          <>
            <Progress
              percent={usageView.percent !== null ? Math.round(usageView.percent) : 0}
              status={usageView.percent !== null && usageView.percent > 90 ? 'exception' : 'normal'}
            />
            <Typography.Text>
              已用 {fmtCreditExact(usageView.used)} / {usageView.limit ? fmtCredit(usageView.limit) : '不限额'} · 剩余{' '}
              {usageView.limit ? fmtCreditExact(usageView.remaining) : '不限'}
            </Typography.Text>
          </>
        )}
      </Modal>

      {/* 来源 IP 弹窗 */}
      <KeyIpsModal item={ipsFor} onClose={() => setIpsFor(null)} />
    </Space>
  )
}

/** Key 来源 IP 详情（5 秒自动刷新，对应旧版 WBKeyIP） */
function KeyIpsModal({ item, onClose }: { item: KeyItem | null; onClose: () => void }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null)
  useEffect(() => {
    if (!item) return
    let stop = false
    const load = async () => {
      try {
        const r = await api<Record<string, unknown>>(`/api/keys/${item.id}/ips?days=7`)
        if (!stop) setData(r)
      } catch {
        /* ignore */
      }
    }
    void load()
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, 5000)
    return () => {
      stop = true
      window.clearInterval(timer)
    }
  }, [item])

  const ips = (data?.items ?? []) as { ip: string; requests: number; last_seen?: string; active_requests?: number }[]
  return (
    <Modal title={`来源 IP：${item?.name ?? ''}`} open={!!item} onCancel={onClose} footer={null} width={640}>
      <Table
        size="small"
        rowKey="ip"
        dataSource={ips}
        pagination={{ pageSize: 10 }}
        columns={[
          { title: 'IP', dataIndex: 'ip', render: (v) => <Typography.Text code>{v}</Typography.Text> },
          { title: '请求数', dataIndex: 'requests', width: 90, align: 'right', render: (v) => <span className="num">{fmtInt(v)}</span> },
          { title: '活跃', dataIndex: 'active_requests', width: 70, align: 'right', render: (v) => (v ? <Tag color="processing">{fmtInt(v)}</Tag> : FMT_EMPTY) },
          { title: '最近', dataIndex: 'last_seen', width: 130, render: (v) => <span className="num">{fmtTime(v, true)}</span> },
        ]}
      />
    </Modal>
  )
}
