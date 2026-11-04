import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { flushSync } from 'react-dom'

export type ThemeMode = 'light' | 'dark'

interface ThemeCtx {
  mode: ThemeMode
  /** 切换主题；传入点击事件/坐标时走「从点击处圆形扩散」过渡（View Transitions） */
  toggle: (origin?: { x: number; y: number } | { clientX: number; clientY: number } | Event) => void
}

const Ctx = createContext<ThemeCtx>({ mode: 'light', toggle: () => {} })
const LS_KEY = 'wb_console_theme'

function systemMode(): ThemeMode {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function initialMode(): ThemeMode {
  const saved = localStorage.getItem(LS_KEY)
  // 未手动切换过 → 跟随系统；手动切换后记住用户选择
  if (saved !== 'dark' && saved !== 'light') return systemMode()
  return saved
}

/** View Transitions API（旧浏览器没有 → 回落瞬切） */
type ViewTransition = { ready: Promise<void> }
type ViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void | Promise<void>) => ViewTransition
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(initialMode)

  useEffect(() => {
    document.documentElement.dataset.theme = mode
  }, [mode])

  // 系统深浅色变化：仅在用户未手动选择过主题时自动跟随
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => {
      if (!localStorage.getItem(LS_KEY)) setMode(e.matches ? 'dark' : 'light')
    }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // 跨标签页同步（storage 事件）
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === LS_KEY && (e.newValue === 'dark' || e.newValue === 'light')) {
        setMode(e.newValue)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // 安卓桥反向同步主题
  useEffect(() => {
    void window.WBAndroid?.setTheme?.(mode)?.catch?.(() => {})
  }, [mode])

  const value = useMemo<ThemeCtx>(
    () => ({
      mode,
      toggle: (origin?: { x: number; y: number } | { clientX: number; clientY: number } | Event) => {
        const next: ThemeMode = mode === 'dark' ? 'light' : 'dark'
        const apply = () => {
          localStorage.setItem(LS_KEY, next)
          // data-theme 同步落 DOM：View Transition 的新快照必须在回调返回前就画好目标主题
          document.documentElement.dataset.theme = next
        }
        const doc = document as ViewTransitionDocument
        // 不支持 View Transitions（或用户偏好减少动效）→ 直接瞬切
        if (!doc.startViewTransition || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
          apply()
          setMode(next)
          return
        }
        // ---- antd 官网同款做法 ----
        // flushSync 保证 React 在回调返回前完成提交，否则新快照仍是旧主题 → 组件闪一下
        const transition = doc.startViewTransition(() => {
          flushSync(() => {
            apply()
            setMode(next)
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
      },
    }),
    [mode],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useThemeMode(): ThemeCtx {
  return useContext(Ctx)
}
