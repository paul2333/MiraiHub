import { api } from './client'

/** /api/platforms 下发的平台元信息（前端零平台知识，全部由后端声明） */
export interface PlatformSpec {
  id: string
  label: string
  icon?: string
  /** 单区平台的固定区域 */
  realm?: string | null
  realm_label?: string | null
  /** 多区平台可用区域 */
  realms?: { key: string; label: string; default?: boolean }[]
  realm_labels?: Record<string, string>
  notes?: string
  account_count?: number
  realm_counts?: Record<string, number>
  /** 平台动作能力由后端声明：begin-auth/scan/import/refresh-quota/checkin/usage/web-cookie */
  actions?: string[]
  storage_platform?: string
  supports_local_scan?: boolean
}

export interface PlatformOverviewCard {
  title?: string
  label?: string
  value?: unknown
  hint?: string
  tone?: string
  [k: string]: unknown
}

export function listPlatforms() {
  return api<{ items: PlatformSpec[]; local_client_features?: boolean }>('/api/platforms', { skipPlatform: true })
}

export function platformOverview(id: string) {
  return api<{ cards?: PlatformOverviewCard[]; items?: PlatformOverviewCard[]; [k: string]: unknown }>(
    `/api/platforms/${encodeURIComponent(id)}/overview`,
  )
}

export interface PlatformSpeedRow {
  platform?: string
  label?: string
  requests?: number
  success_rate?: number
  p50?: number
  p95?: number
  tps?: number
  [k: string]: unknown
}

export function platformSpeed(hours = 24) {
  return api<{ items?: PlatformSpeedRow[]; rows?: PlatformSpeedRow[]; [k: string]: unknown }>(
    `/api/platform/speed?hours=${hours}`,
  )
}
