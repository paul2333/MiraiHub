import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { flushSync } from 'react-dom'

export type ThemeMode = 'light' | 'dark'
/** 用户偏好：auto = 跟随系统（自适应） */
export type ThemePreference = ThemeMode | 'auto'

interface ThemeCtx {
  /** 实际生效的模式（auto 已解析成浅/深，组件只认它） */
  mode: ThemeMode
  /** 用户偏好（浅色 / 深色 / 自适应），右上角组合按钮按它打勾 */
  preference: ThemePreference
  /** 设置偏好；传入点击事件/坐标时走「从点击处圆形扩散」过渡 */
  setPreference: (p: ThemePreference, origin?: ToggleOrigin) => void
  /** 浅↔深快捷切换（保留给老调用方） */
  toggle: (origin?: ToggleOrigin) => void
}

type ToggleOrigin = { x?: number; y?: number; clientX?: number; clientY?: number }

const Ctx = createContext<ThemeCtx>({ mode: 'light', preference: 'auto', setPreference: () => {}, toggle: () => {} })
const LS_KEY = 'wb_console_theme'

function systemMode(): ThemeMode {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function initialPreference(): ThemePreference {
  const saved = localStorage.getItem(LS_KEY)
  // 未手动选择过（或存了 auto）→ 自适应跟随系统；light/dark 为手动锁定
  if (saved === 'light' || saved === 'dark' || saved === 'auto') return saved
  return 'auto'
}

/** View Transitions API（旧浏览器没有 → 回落瞬切） */
type ViewTransition = { ready: Promise<void> }
type ViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void | Promise<void>) => ViewTransition
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(initialPreference)
  // 系统深浅色单独存一份：preference==='auto' 时 mode 跟着它走
  const [system, setSystem] = useState<ThemeMode>(systemMode)
  const mode: ThemeMode = preference === 'auto' ? system : preference

  useEffect(() => {
    document.documentElement.dataset.theme = mode
  }, [mode])

  // 系统深浅色变化：跟随系统偏好时实时切换
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => setSystem(e.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // 跨标签页同步（storage 事件）
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === LS_KEY && (e.newValue === 'dark' || e.newValue === 'light' || e.newValue === 'auto')) {
        setPreferenceState(e.newValue)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // 安卓桥反向同步主题（桥只认浅/深，auto 传解析后的生效值）
  useEffect(() => {
    void window.WBAndroid?.setTheme?.(mode)?.catch?.(() => {})
  }, [mode])

  const value = useMemo<ThemeCtx>(
    () => {
      const setPreference = (next: ThemePreference, origin?: ToggleOrigin) => {
        if (next === preference) return
        const apply = () => {
          localStorage.setItem(LS_KEY, next)
        }
        const doc = document as ViewTransitionDocument
        // 不支持 View Transitions（或用户偏好减少动效）→ 直接瞬切
        if (!doc.startViewTransition || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
          apply()
          setPreferenceState(next)
          return
        }
        // ---- antd 官网同款做法 ----
        // flushSync 保证 React 在回调返回前完成提交，否则新快照仍是旧主题 → 组件闪一下
        const transition = doc.startViewTransition(() => {
          flushSync(() => {
            apply()
            setPreferenceState(next)
          })
        })
        // 从点击位置圆形扩散揭示新主题（clip-path 动画打在 ::view-transition-new 上）
        void transition.ready
          .then(() => {
            const pt = origin as { x?: number; y?: number; clientX?: number; clientY?: number } | undefined
            const x = pt?.x ?? pt?.clientX ?? window.innerWidth - 40
            const y = pt?.y ?? pt?.clientY ?? 24
            const endRadius = Math.hypot(
              Math.max(x, window.innerWidth - x),
              Math.max(y, window.innerHeight - y),
            )
            document.documentElement.animate(
              {
                clipPath: [
                  `circle(0px at ${x}px ${y}px)`,
                  `circle(${endRadius}px at ${x}px ${y}px)`,
                ],
              },
              {
                duration: 450,
                easing: 'ease-in-out',
                pseudoElement: '::view-transition-new(root)',
              },
            )
          })
          .catch(() => {})
      }
      return {
        mode,
        preference,
        setPreference,
        toggle: (origin?: ToggleOrigin) => {
          // auto 下按当前生效值反向锁定
          setPreference(mode === 'dark' ? 'light' : 'dark', origin)
        },
      }
    },
    [mode, preference],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useThemeMode(): ThemeCtx {
  return useContext(Ctx)
}
