import { App as AntdApp, Button, Input, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { CloudDownloadOutlined, KeyOutlined, ImportOutlined, LoginOutlined, PieChartOutlined, RocketOutlined, ScanOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useState } from 'react'
import type { PlatformSpec } from '../api/platforms'
import {
  platformCheckin,
  platformImportLocal,
  platformRefreshAll,
  platformScanLocal,
  platformUsage,
  traeWebCookie,
} from '../api/platformOps'
import DeviceAuthModal from './DeviceAuthModal'
import { WbUsageDrawer } from '../pages/accounts/WbUsageDrawer'
import { usePlatform } from '../context/PlatformContext'

const ACTION_LABELS: Record<string, { label: string; icon: React.ReactNode; tip?: string }> = {
  growth: { label: '成长计划', icon: <RocketOutlined />, tip: '在浏览器小窗中打开' },
  'begin-auth': { label: '授权接入', icon: <LoginOutlined />, tip: '浏览器/设备授权登录' },
  scan: { label: '扫描本机', icon: <ScanOutlined />, tip: '扫描本机客户端登录态' },
  import: { label: '导入本机', icon: <ImportOutlined />, tip: '把扫描到的凭证导入号池' },
  'refresh-quota': { label: '刷新额度', icon: <ThunderboltOutlined />, tip: '批量刷新全部账号额度' },
  checkin: { label: '签到', icon: <PieChartOutlined />, tip: '执行每日签到领权益' },
  usage: { label: '用量明细', icon: <CloudDownloadOutlined />, tip: '查看账号用量明细' },
  'web-cookie': { label: '用量 Cookie', icon: <KeyOutlined />, tip: '配置网页会话 Cookie 读取用量' },
}

/** 平台动作按钮区（能力由 /api/platforms 的 spec.actions 声明） */
export default function PlatformActionsBar({ spec, onChanged }: { spec?: PlatformSpec; onChanged?: () => void }) {
  const { message, modal } = AntdApp.useApp()
  const { current, refreshCounts, localFeatures } = usePlatform()
  const [busy, setBusy] = useState('')
  const [authOpen, setAuthOpen] = useState(false)
  const [cookieOpen, setCookieOpen] = useState(false)
  const [cookie, setCookie] = useState('')
  const [usageOpen, setUsageOpen] = useState(false)

  if (!spec) return null
  const storage = (spec.storage_platform || spec.id).toLowerCase()
  const realm = current.realm || undefined
  const actions = spec.actions || []

/** 从结果对象推断动态列（只取基本类型字段，对象字段 JSON 序列化展示） */
const dynCols = (row: Record<string, unknown>) =>
  Object.keys(row ?? {})
    .slice(0, 8)
    .map((k) => ({
      title: k,
      dataIndex: k,
      ellipsis: true,
      render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '—')),
    }))

const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    try {
      await fn()
    } catch (e) {
      message.error((e as Error).message || `${ACTION_LABELS[name]?.label ?? name}失败`)
    } finally {
      setBusy('')
    }
  }

  const doScan = () =>
    run('scan', async () => {
      const r = await platformScanLocal(storage, realm)
      const o = r as Record<string, unknown>
      const accounts = Array.isArray(o.accounts) ? (o.accounts as Record<string, unknown>[]) : []
      const found = Number(o.found ?? o.count ?? accounts.length ?? 0)
      if (accounts.length) {
        // 扫描结果表格化：账号 / 区域 / 状态一目了然，不再糊一屏 JSON
        modal.info({
          title: `扫描完成：发现 ${found} 个本机登录态`,
          width: 680,
          content: (
            <Table
              size="small"
              rowKey={(_, i) => String(i)}
              dataSource={accounts.slice(0, 100)}
              pagination={{ pageSize: 8 }}
              columns={dynCols(accounts[0])}
            />
          ),
        })
      } else {
        modal.info({
          title: '扫描完成',
          content: (
            <Typography.Text type="secondary">
              {typeof o.summary === 'string' && o.summary ? o.summary : `未发现本机登录态（${storage.toUpperCase()}）`}
            </Typography.Text>
          ),
          width: 560,
        })
      }
    })

  const doImport = () =>
    run('import', async () => {
      const r = await platformImportLocal(storage, realm)
      const o = r as Record<string, unknown>
      message.success(`导入完成：新增 ${Number(o.added ?? 0)}，更新 ${Number(o.updated ?? 0)}${o.errors ? `，失败 ${Array.isArray(o.errors) ? o.errors.length : 0}` : ''}`)
      await refreshCounts()
      onChanged?.()
    })

  const doRefresh = () =>
    run('refresh-quota', async () => {
      const r = await platformRefreshAll(storage, realm)
      const o = r as { ok?: number; failed?: number; items?: { name: string; ok: boolean; error?: string }[] }
      modal.info({
        title: `额度刷新完成：成功 ${o.ok ?? 0}，失败 ${o.failed ?? 0}`,
        width: 560,
        content: (
          <Table
            size="small"
            rowKey={(i) => String(i.name)}
            dataSource={o.items ?? []}
            pagination={{ pageSize: 8 }}
            columns={[
              { title: '账号', dataIndex: 'name' },
              { title: '结果', dataIndex: 'ok', width: 80, render: (v) => (v ? <Tag color="success">成功</Tag> : <Tag color="error">失败</Tag>) },
              { title: '原因', dataIndex: 'error', ellipsis: true },
            ]}
          />
        ),
      })
    })

  const doCheckin = () =>
    run('checkin', async () => {
      const r = await platformCheckin(storage, realm)
      const o = r as Record<string, unknown>
      // 平台没有签到接口：把原因讲清楚，而不是糊一段 JSON
      if (o.supported === false) {
        modal.info({
          title: '签到',
          content: (
            <Typography.Text type="secondary">
              {String(o.summary || '该平台没有签到接口')}
            </Typography.Text>
          ),
          width: 520,
        })
        return
      }
      const items = (Array.isArray(o.items) ? o.items : Array.isArray(o.results) ? o.results : []) as Record<string, unknown>[]
      const okN = Number(o.ok ?? o.granted ?? 0)
      const failN = Number(o.failed ?? 0)
      const line = typeof o.summary === 'string' && o.summary
        ? o.summary
        : `成功 ${okN}，失败 ${failN}${o.already ? `，已领过 ${Number(o.already)}` : ''}`
      modal.info({
        title: '签到完成',
        width: 680,
        content: (
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text>{line}</Typography.Text>
            {items.length > 0 && (
              <Table
                size="small"
                rowKey={(_, i) => String(i)}
                dataSource={items.slice(0, 100)}
                pagination={{ pageSize: 8 }}
                columns={dynCols(items[0])}
              />
            )}
          </Space>
        ),
      })
    })

  const doUsage = () => {
    setUsageOpen(true)
    void run('usage', async () => {
      /* 数据在 UsageModal 内部拉取 */
    })
  }

  const saveCookie = () =>
    run('web-cookie', async () => {
      const c = cookie.trim()
      if (!c) {
        message.warning('请先粘贴 Cookie')
        return
      }
      const r = await traeWebCookie(c)
      message.success(`已保存到 ${(r.updated || []).length} 个账号`)
      setCookieOpen(false)
    })

  return (
    <Space wrap size={8}>
      {(storage === 'workbuddy' ? [...actions, 'growth'] : actions)
        // 本机客户端功能关闭：扫描/导入入口随之隐藏（服务器部署没有本机客户端）
        .filter((a) => localFeatures || (a !== 'scan' && a !== 'import'))
        .map((a) => {
        const meta = ACTION_LABELS[a]
        if (!meta) return null
        return (
          <Tooltip key={a} title={meta.tip}>
            <Button
              icon={meta.icon}
              loading={busy === a}
              onClick={() => {
                if (a === 'begin-auth') setAuthOpen(true)
                else if (a === 'scan') void doScan()
                else if (a === 'import') void doImport()
                else if (a === 'refresh-quota') void doRefresh()
                else if (a === 'checkin') void doCheckin()
                else if (a === 'usage') doUsage()
                else if (a === 'web-cookie') setCookieOpen(true)
                else if (a === 'growth') window.open(`${window.location.origin}/admin#/growth-popup`, 'mh-growth', 'width=1120,height=780,noopener')
              }}
            >
              {meta.label}
            </Button>
          </Tooltip>
        )
      })}
      <DeviceAuthModal
        open={authOpen}
        storage={storage}
        realm={realm}
        onClose={() => setAuthOpen(false)}
        onDone={() => {
          void refreshCounts()
          onChanged?.()
        }}
      />
      <Modal
        title="用量 Cookie（Trae 网页会话）"
        open={cookieOpen}
        onCancel={() => setCookieOpen(false)}
        onOk={() => void saveCookie()}
        okText="保存"
        width={640}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          浏览器登录 www.trae.cn（国际版为 www.trae.ai）→ F12 → Network → 任意请求 → 复制 Cookie 请求头整行粘贴到下面。仅存本地数据库用于读取用量。
        </Typography.Paragraph>
        <Input.TextArea
          rows={5}
          value={cookie}
          onChange={(e) => setCookie(e.target.value)}
          placeholder="sid_tt=...; sessionid=...; X-Cloudide-Session=..."
        />
      </Modal>
      {storage === 'workbuddy' ? (
        <WbUsageDrawer open={usageOpen} onClose={() => setUsageOpen(false)} />
      ) : (
        <UsageDrawer storage={storage} realm={realm} open={usageOpen} onClose={() => setUsageOpen(false)} />
      )}
    </Space>
  )
}

/** 用量明细弹窗（GET /api/{storage}/usage?realm=&days=） */
function UsageDrawer({ storage, realm, open, onClose }: { storage: string; realm?: string; open: boolean; onClose: () => void }) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await platformUsage(storage, realm)
      const o = r as Record<string, unknown>
      const items = (o.items ?? o.rows ?? o.accounts ?? o.usage ?? []) as Record<string, unknown>[]
      setRows(Array.isArray(items) ? items : [])
    } catch (e) {
      setErr((e as Error).message || '该平台暂不支持用量查询')
      setRows([])
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal
      title="用量明细"
      open={open}
      onCancel={onClose}
      footer={null}
      width={760}
      afterOpenChange={(v) => {
        if (v) void load()
      }}
    >
      {err && <Typography.Text type="danger">{err}</Typography.Text>}
      <Table
        size="small"
        loading={loading}
        rowKey={(_, i) => String(i)}
        dataSource={rows}
        pagination={{ pageSize: 10 }}
        columns={
          rows[0]
            ? Object.keys(rows[0])
                .slice(0, 8)
                .map((k) => ({
                  title: k,
                  dataIndex: k,
                  ellipsis: true,
                  render: (v: unknown) => (typeof v === 'object' ? JSON.stringify(v) : String(v ?? '—')),
                }))
            : []
        }
      />
    </Modal>
  )
}
