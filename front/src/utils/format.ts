import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'

dayjs.extend(utc)

export const FMT_EMPTY = '—'

/** 后端返回 naive UTC 字符串（"YYYY-MM-DD HH:mm:ss"）→ 北京时间显示 */
export function fmtTime(v?: string | null, withSeconds = false): string {
  if (!v) return FMT_EMPTY
  const d = dayjs.utc(v)
  if (!d.isValid()) return v
  return d.utcOffset(8).format(withSeconds ? 'YYYY-MM-DD HH:mm:ss' : 'MM-DD HH:mm')
}

export function fmtTimeFull(v?: string | null): string {
  return fmtTime(v, true)
}

/** Unix 时间戳（秒或毫秒自适应）→ 北京时间显示 */
export function fmtEpoch(v?: number | null, withSeconds = false): string {
  if (!v) return FMT_EMPTY
  const ms = v < 1e12 ? v * 1000 : v
  return dayjs(ms).utcOffset(8).format(withSeconds ? 'YYYY-MM-DD HH:mm:ss' : 'MM-DD HH:mm')
}

/** 北京时间「今日」零点（对应旧版 cnMidnight） */
export function cnMidnight(): dayjs.Dayjs {
  return dayjs().utcOffset(8).startOf('day')
}

export function beijingToday(): string {
  return dayjs().utcOffset(8).format('YYYY-MM-DD')
}

export function fmtDuration(ms?: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return FMT_EMPTY
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}分${Math.round((ms % 60_000) / 1000)}秒`
  return `${Math.floor(ms / 3_600_000)}时${Math.round((ms % 3_600_000) / 60_000)}分`
}

export function fmtPct(v?: number | null, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  return `${(v * 100).toFixed(digits)}%`
}

export function fmtNum(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  return new Intl.NumberFormat('zh-CN').format(v)
}

export function fmtInt(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  return String(Math.round(v))
}

export function fmtK(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (Math.abs(v) >= 10_000) return `${(v / 1000).toFixed(1)}k`
  return fmtNum(v)
}

export function fmtCredit(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  return fmtNum(Math.round(v))
}

export function fmtCreditExact(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  return v.toFixed(2)
}

export function fmtAuto(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return FMT_EMPTY
  if (Number.isInteger(v)) return fmtNum(v)
  return v.toFixed(2)
}

/** 相对时间："3 分钟前" */
export function relTime(ts?: string | number | null): string {
  if (!ts) return FMT_EMPTY
  const t = typeof ts === 'number' ? ts : dayjs.utc(ts).valueOf()
  if (!t) return FMT_EMPTY
  const diff = Date.now() - t
  if (diff < 0) return '刚刚'
  if (diff < 60_000) return `${Math.max(1, Math.floor(diff / 1000))} 秒前`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return `${Math.floor(diff / 86_400_000)} 天前`
}

export function secondsAfter(ts?: string | number | null): number | null {
  if (!ts) return null
  const t = typeof ts === 'number' ? ts : dayjs.utc(ts).valueOf()
  if (!t) return null
  return Math.max(0, Math.round((t - Date.now()) / 1000))
}

/** 导出文件名时间戳：YYYYMMDD-HHmm（对应旧版 exportStamp） */
export function exportStamp(): string {
  return dayjs().format('YYYYMMDD-HHmm')
}

export function toISOStringLocal(d: dayjs.Dayjs): string {
  return d.utcOffset(8).format('YYYY-MM-DDTHH:mm:ss')
}
