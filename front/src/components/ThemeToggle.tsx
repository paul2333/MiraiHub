import { Button, Dropdown, Tooltip } from 'antd'
import { CheckOutlined, DesktopOutlined, MoonOutlined, SunOutlined } from '@ant-design/icons'
import { useThemeMode, type ThemePreference } from '../context/ThemeContext'

const OPTIONS: { key: ThemePreference; label: string; icon: React.ReactNode }[] = [
  { key: 'light', label: '浅色模式', icon: <SunOutlined /> },
  { key: 'dark', label: '深色模式', icon: <MoonOutlined /> },
  { key: 'auto', label: '自适应（跟随系统）', icon: <DesktopOutlined /> },
]

/** 右上角主题切换组合按钮：浅色 / 深色 / 自适应（菜单打勾标当前项，切换带圆形扩散动画） */
export default function ThemeToggle() {
  const { mode, preference, setPreference } = useThemeMode()
  const icon = preference === 'auto' ? <DesktopOutlined /> : mode === 'dark' ? <MoonOutlined /> : <SunOutlined />
  return (
    <Dropdown
      trigger={['click']}
      placement="bottomRight"
      menu={{
        items: OPTIONS.map((o) => ({
          key: o.key,
          icon: o.icon,
          label: o.label,
          extra: preference === o.key ? <CheckOutlined style={{ color: '#2E9BFF' }} /> : undefined,
        })),
        selectedKeys: [preference],
        onClick: ({ key, domEvent }) => {
          // 键盘触发时 clientX/clientY 为 undefined → 动画坐标回落到右上角
          const pt = domEvent as unknown as { clientX?: number; clientY?: number }
          setPreference(key as ThemePreference, { clientX: pt.clientX, clientY: pt.clientY })
        },
      }}
    >
      <Tooltip title="主题：浅色 / 深色 / 自适应">
        <Button type="text" size="small" icon={icon} />
      </Tooltip>
    </Dropdown>
  )
}
