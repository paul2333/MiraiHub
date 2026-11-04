import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { listPlatforms, type PlatformSpec } from '../api/platforms'
import {
  getPlatformHeader,
  getPlatformRoute,
  getPlatformState,
  setPlatformState,
  subscribePlatform,
  type PlatformRoute,
} from '../api/platformBus'
import { useAuth } from './AuthContext'

interface PlatformCtx {
  platforms: PlatformSpec[]
  /** 本机客户端功能总开关（后台「设置」可控）：关闭时隐藏逆向产物 / 扫描本机 / 导入本机 */
  localFeatures: boolean
  current: PlatformRoute
  /** 当前平台路由值（header 值），平台切换后变化 → 各页面依赖它重拉 */
  platformKey: string
  loading: boolean
  reload: () => Promise<void>
  /** 重新拉平台列表（导入/授权后刷新账号数角标） */
  refreshCounts: () => Promise<void>
  select: (platform: string, realm?: string | null) => void
  /** 平台动作请求（授权接入/扫描/导入/刷新额度/打卡/用量），由账号页提供实现 */
  requestAction: (action: string) => void
  setActionHandler: (fn: ((action: string) => void) | null) => void
}

const Ctx = createContext<PlatformCtx | null>(null)

export function PlatformProvider({ children }: { children: ReactNode }) {
  const { authed } = useAuth()
  const [platforms, setPlatforms] = useState<PlatformSpec[]>([])
  const [localFeatures, setLocalFeatures] = useState(true)
  const [current, setCurrent] = useState<PlatformRoute>(() => getPlatformState())
  const [loading, setLoading] = useState(false)
  const [handler, setHandler] = useState<((action: string) => void) | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await listPlatforms()
      setPlatforms(resp?.items ?? [])
      setLocalFeatures(resp?.local_client_features !== false)
      // 当前平台若已不存在（如切换部署），回落到第一个平台
      const cur = getPlatformState()
      if (resp?.items?.length && !resp.items.some((p) => p.id === cur.platform)) {
        const first = resp.items[0]
        setPlatformState({ platform: first.id, realm: first.realm ?? first.realms?.[0]?.key ?? null })
        setCurrent(getPlatformState())
      }
    } catch {
      /* 平台列表失败不阻塞页面 */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!authed) {
      setPlatforms([])
      return
    }
    void reload()
  }, [authed, reload])

  // 平台总线 → React 状态
  useEffect(() => subscribePlatform(() => setCurrent(getPlatformState())), [])

  const select = useCallback((platform: string, realm?: string | null) => {
    setPlatformState({ platform, realm: realm ?? null })
  }, [])

  const requestAction = useCallback(
    (action: string) => {
      handler?.(action)
    },
    [handler],
  )

  const value = useMemo<PlatformCtx>(
    () => ({
      platforms,
      localFeatures,
      current,
      platformKey: getPlatformRoute(),
      loading,
      reload,
      refreshCounts: reload,
      select,
      requestAction,
      setActionHandler: setHandler,
    }),
    [platforms, localFeatures, current, loading, reload, select, requestAction],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function usePlatform(): PlatformCtx {
  const v = useContext(Ctx)
  if (!v) throw new Error('usePlatform 必须在 PlatformProvider 内使用')
  return v
}

export { getPlatformHeader }
