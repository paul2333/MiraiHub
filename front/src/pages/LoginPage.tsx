import { App as AntdApp, Avatar, Button, Flex, Form, Input, Typography, theme } from 'antd'
import {
  AimOutlined,
  CloudServerOutlined,
  LockOutlined,
  SafetyOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useEffect, useState, type ReactNode } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import ThemeToggle from '../components/ThemeToggle'
import { useAuth } from '../context/AuthContext'
import { useThemeMode } from '../context/ThemeContext'
import { ensureLocalSession, isDesktopMode } from '../bridges/native'
import { setToken } from '../api/client'

/** 品牌区特性列表（左侧渐变面板内） */
const FEATURES: { icon: ReactNode; title: string; desc: string }[] = [
  { icon: <CloudServerOutlined />, title: '多平台聚合调度', desc: 'WorkBuddy · Trae · Qoder · Loomy 一个 Key 全覆盖' },
  { icon: <AimOutlined />, title: '三协议互转', desc: 'OpenAI Chat / Responses / Anthropic Messages 自动适配' },
  { icon: <SafetyOutlined />, title: '风控与配额', desc: '账号池自动冷却、封号复核、用量与额度监控' },
]

export default function LoginPage() {
  const { authed, ready, login } = useAuth()
  const { mode } = useThemeMode()
  const { token } = theme.useToken()
  const { message } = AntdApp.useApp()
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const dark = mode === 'dark'

  // 桌面/安卓本地壳：无需登录表单，直接走桥换取本地 token
  useEffect(() => {
    if (!ready || authed) return
    if (!isDesktopMode()) return
    void ensureLocalSession(true).then((t) => {
      if (t) {
        setToken(t)
        window.location.reload()
      }
    })
  }, [ready, authed])

  if (ready && authed) return <Navigate to="/overview" replace />

  const onFinish = async ({ username, password }: { username: string; password: string }) => {
    setSubmitting(true)
    try {
      await login(username, password)
      message.success('登录成功')
      navigate('/overview', { replace: true })
    } catch (e) {
      message.error((e as Error).message || '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Flex style={{ minHeight: '100vh', background: token.colorBgLayout }}>
      <div style={{ position: 'fixed', top: 12, right: 12, zIndex: 10 }}>
        <ThemeToggle />
      </div>

      {/* 左侧品牌区（窄屏隐藏，见 index.css .login-hero） */}
      <div
        className="login-hero"
        style={{
          background: dark
            ? 'linear-gradient(150deg, #16202e 0%, #101826 55%, #0d131d 100%)'
            : 'linear-gradient(150deg, #2e9bff 0%, #1f7fe0 55%, #1668c4 100%)',
        }}
      >
        <div className="login-hero-grid" />
        <div
          className="login-hero-glow"
          style={{ top: '16%', right: '10%', width: 260, height: 260, background: dark ? 'rgba(46,155,255,0.16)' : 'rgba(255,255,255,0.28)' }}
        />
        <div
          className="login-hero-glow"
          style={{ bottom: '6%', left: '18%', width: 400, height: 400, background: dark ? 'rgba(46,155,255,0.1)' : 'rgba(255,255,255,0.18)' }}
        />

        <Flex align="center" gap={10} style={{ position: 'relative', zIndex: 2 }}>
          <Avatar shape="square" size={40} src="/api/app/icon" style={{ background: 'rgba(255,255,255,0.14)' }} />
          <Typography.Text strong style={{ fontSize: 18, color: '#fff' }}>
            MiraiHub
          </Typography.Text>
        </Flex>

        <div style={{ position: 'relative', zIndex: 2, maxWidth: 460 }}>
          <Typography.Title level={3} style={{ color: '#fff', marginBottom: 6 }}>
            一个 API Key，调用多家上游
          </Typography.Title>
          <Typography.Paragraph style={{ color: 'rgba(255,255,255,0.72)', fontSize: 13.5, marginBottom: 28 }}>
            聚合网关自动选号、限流与记账，对客户端透明 —— 换平台不改一行代码。
          </Typography.Paragraph>
          <Flex vertical gap={18}>
            {FEATURES.map((f) => (
              <Flex key={f.title} gap={12} align="flex-start">
                <span
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 8,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 16,
                    flexShrink: 0,
                    background: 'rgba(255,255,255,0.14)',
                    color: '#fff',
                  }}
                >
                  {f.icon}
                </span>
                <span>
                  <Typography.Text strong style={{ display: 'block', color: '#fff', fontSize: 13.5 }}>
                    {f.title}
                  </Typography.Text>
                  <Typography.Text style={{ display: 'block', color: 'rgba(255,255,255,0.66)', fontSize: 12 }}>
                    {f.desc}
                  </Typography.Text>
                </span>
              </Flex>
            ))}
          </Flex>
        </div>

        <Flex gap={24} style={{ position: 'relative', zIndex: 2, fontSize: 12, color: dark ? 'rgba(255,255,255,0.4)' : 'rgba(255,255,255,0.62)' }}>
          <span>调度网关</span>
          <span>风控哨兵</span>
          <span>用量监控</span>
        </Flex>
      </div>

      {/* 右侧表单区（窄屏占满整行） */}
      <Flex flex={1} align="center" justify="center" style={{ padding: '48px 16px' }}>
        <div style={{ width: '100%', maxWidth: 420 }}>
          {/* 移动端顶部 logo（宽屏隐藏） */}
          <Flex align="center" justify="center" gap={10} className="login-mobile-logo" style={{ marginBottom: 40 }}>
            <Avatar shape="square" size={40} src="/api/app/icon" />
            <Typography.Text strong style={{ fontSize: 18 }}>
              MiraiHub
            </Typography.Text>
          </Flex>

          <Typography.Title level={3} style={{ marginBottom: 4 }}>
            欢迎回来
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            请输入管理凭证登录控制台
          </Typography.Text>

          <Form layout="vertical" onFinish={onFinish} autoComplete="off" style={{ marginTop: 28 }}>
            <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
              <Input size="large" prefix={<UserOutlined />} placeholder="管理用户名" autoFocus />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password size="large" prefix={<LockOutlined />} placeholder="••••••••" autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large" loading={submitting} style={{ marginTop: 4 }}>
              登录
            </Button>
          </Form>

          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 16 }}>
            连续失败会触发登录保护，请稍后再试。
          </Typography.Text>
        </div>
      </Flex>
    </Flex>
  )
}
