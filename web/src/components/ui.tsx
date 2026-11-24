import { useEffect, useRef, useState, type ReactNode } from 'react'

/* ═══════════════════════════════════════════════════════════════ icons
   Inline SVG so the app has no icon-font dependency and works offline. */

type IconProps = { size?: number; className?: string }
const svg = (size: number, children: ReactNode, className?: string) => (
  <svg
    width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    className={className} aria-hidden="true"
  >{children}</svg>
)

export const Icon = {
  dashboard: ({ size = 16, className }: IconProps) => svg(size, <>
    <rect x="3" y="3" width="7" height="9" rx="1.5" />
    <rect x="14" y="3" width="7" height="5" rx="1.5" />
    <rect x="14" y="12" width="7" height="9" rx="1.5" />
    <rect x="3" y="16" width="7" height="5" rx="1.5" />
  </>, className),

  pipelines: ({ size = 16, className }: IconProps) => svg(size, <>
    <circle cx="6" cy="6" r="2.4" /><circle cx="6" cy="18" r="2.4" /><circle cx="18" cy="12" r="2.4" />
    <path d="M6 8.4v7.2M8.4 6H13a2.6 2.6 0 0 1 2.6 2.6v1M8.4 18H13a2.6 2.6 0 0 0 2.6-2.6v-1" />
  </>, className),

  bulb: ({ size = 16, className }: IconProps) => svg(size, <>
    <path d="M9 18h6M10 21h4" />
    <path d="M12 3a6 6 0 0 0-3.6 10.8c.5.4.8 1 .8 1.6v.6h5.6v-.6c0-.6.3-1.2.8-1.6A6 6 0 0 0 12 3Z" />
  </>, className),

  flaky: ({ size = 16, className }: IconProps) => svg(size, <>
    <path d="M10.3 3.9 2.6 17.1A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.9L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4M12 16.5v.01" />
  </>, className),

  policies: ({ size = 16, className }: IconProps) => svg(size, <>
    <path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h10M18 18h2" />
    <circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="16" cy="18" r="2" />
  </>, className),

  models: ({ size = 16, className }: IconProps) => svg(size, <>
    <rect x="8" y="8" width="8" height="8" rx="2" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
  </>, className),

  logout: ({ size = 15, className }: IconProps) => svg(size, <>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
  </>, className),

  chevron: ({ size = 14, className }: IconProps) => svg(size, <path d="m6 9 6 6 6-6" />, className),
  check: ({ size = 14, className }: IconProps) => svg(size, <path d="M20 6 9 17l-5-5" />, className),
  x: ({ size = 14, className }: IconProps) => svg(size, <path d="M18 6 6 18M6 6l12 12" />, className),
  arrow: ({ size = 14, className }: IconProps) => svg(size, <path d="M5 12h14M13 6l6 6-6 6" />, className),
  refresh: ({ size = 14, className }: IconProps) => svg(size, <>
    <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6" />
  </>, className),
  info: ({ size = 15, className }: IconProps) => svg(size, <>
    <circle cx="12" cy="12" r="9" /><path d="M12 16v-5M12 8v.01" />
  </>, className),
  alert: ({ size = 15, className }: IconProps) => svg(size, <>
    <circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16v.01" />
  </>, className),
  inbox: ({ size = 19, className }: IconProps) => svg(size, <>
    <path d="M21 12h-6l-2 3h-2l-2-3H3" />
    <path d="M5.4 5.6 3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6l-2.4-6.4A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.9 1.6Z" />
  </>, className),
  clock: ({ size = 13, className }: IconProps) => svg(size, <>
    <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />
  </>, className),
  plug: ({ size = 16, className }: IconProps) => svg(size, <>
    <path d="M9 3v5M15 3v5" />
    <path d="M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8Z" />
    <path d="M12 17v4" />
  </>, className),

  plus: ({ size = 14, className }: IconProps) => svg(size,
    <path d="M12 5v14M5 12h14" />, className),

  zap: ({ size = 14, className }: IconProps) => svg(size,
    <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />, className),
  shield: ({ size = 14, className }: IconProps) => svg(size, <>
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" /><path d="m9 12 2 2 4-4" />
  </>, className),
  report: ({ size = 16, className }: IconProps) => svg(size, <>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5M9 17v-3M12 17v-6M15 17v-2" />
  </>, className),
  flag: ({ size = 14, className }: IconProps) => svg(size, <>
    <path d="M5 21V4M5 4h11l-2 4 2 4H5" />
  </>, className),
  download: ({ size = 14, className }: IconProps) => svg(size, <>
    <path d="M12 4v11M7 11l5 5 5-5M5 20h14" />
  </>, className),
  mail: ({ size = 14, className }: IconProps) => svg(size, <>
    <rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" />
  </>, className),
  calendar: ({ size = 14, className }: IconProps) => svg(size, <>
    <rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 10h18M8 3v4M16 3v4" />
  </>, className),
}

/** Фірмовий знак: лінії потоку і тіло попереду них.
 *  Це і є slipstream - рух у сліді, швидше при меншому опорі. */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <div className="brand-mark" style={{ width: size, height: size }}>
      <svg width={size * 0.64} height={size * 0.64} viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
        <path d="M6 7.5h7.5" />
        <path d="M3 12h9.5" />
        <path d="M6.5 16.5h7" />
        <circle cx="18.5" cy="12" r="2.1" fill="currentColor" stroke="none" />
      </svg>
    </div>
  )
}

/** Заголовок секції з акцентною засічкою і лінією, що добігає до краю. */
export function SectionHead({ title, right }: { title: string; right?: ReactNode }) {
  return (
    <div className="section-head">
      <h3>{title}</h3>
      <div className="line" />
      {right}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════ blocks */

export function Tile({ label, value, hint, tone, hero }: {
  label: string; value: ReactNode; hint?: ReactNode
  tone?: 'ok' | 'warn' | 'err' | 'acc'
  /** Головна плитка: більше число й акцентна рамка. Одна на екран. */
  hero?: boolean
}) {
  return (
    <div className={`card tile${hero ? ' accent hero' : ''}`}>
      <div className="label">{label}</div>
      <div className={`value${tone ? ' ' + tone : ''}`}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { cls: string; text: string; pulse?: boolean }> = {
    success: { cls: 'ok', text: 'Passed' },
    failed: { cls: 'err', text: 'Failed' },
    canceled: { cls: 'neutral', text: 'Canceled' },
    running: { cls: 'warn', text: 'Running', pulse: true },
  }
  const m = map[status] ?? { cls: 'neutral', text: status }
  return (
    <span className={`badge ${m.cls}${m.pulse ? ' pulse' : ''}`}>{m.text}</span>
  )
}

export function ModeBadge({ mode }: { mode: string }) {
  const map: Record<string, { cls: string; text: string }> = {
    active: { cls: 'ok', text: 'Active' },
    dry_run: { cls: 'warn', text: 'Dry run' },
    off: { cls: 'neutral', text: 'Observe only' },
    selective: { cls: 'accent', text: 'Selective' },
    sharded: { cls: 'accent', text: 'Sharded' },
    skip: { cls: 'neutral', text: 'Skipped' },
    full: { cls: 'neutral', text: 'Full suite' },
  }
  const m = map[mode] ?? { cls: 'neutral', text: mode }
  return <span className={`badge ${m.cls}`}>{m.text}</span>
}

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div className="card" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton sk-row" style={{ width: `${92 - i * 13}%` }} />
      ))}
    </div>
  )
}

export function TileSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="grid k4" aria-busy="true">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="card tile">
          <div className="skeleton" style={{ height: 10, width: '52%' }} />
          <div className="skeleton" style={{ height: 25, width: '68%', marginTop: 10 }} />
        </div>
      ))}
    </div>
  )
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error" role="alert">
      <Icon.alert />
      <span>{message}</span>
    </div>
  )
}

export function Notice({ children, plain }: { children: ReactNode; plain?: boolean }) {
  return (
    <div className={`notice${plain ? ' plain' : ''}`}>
      <Icon.info />
      <span>{children}</span>
    </div>
  )
}

export function Empty({ title, text }: { title: string; text?: string }) {
  return (
    <div className="card">
      <div className="empty">
        <div className="empty-icon"><Icon.inbox /></div>
        <h4>{title}</h4>
        {text && <p>{text}</p>}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════ format */

/** Seconds rendered the way an engineer reads them. */
export function dur(seconds: number): string {
  if (!seconds || seconds < 0) return '-'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/** ISO date (yyyy-mm-dd) as dd.mm.yyyy. */
export function uaDate(isoDate: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate)
  return m ? `${m[3]}.${m[2]}.${m[1]}` : isoDate
}

/** dd.mm.yyyy back to ISO, or null if it is not a real date. */
function parseUaDate(text: string): string | null {
  const m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(text.trim())
  if (!m) return null
  const iso = `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`
  const d = new Date(iso + 'T00:00:00Z')
  return !isNaN(d.getTime()) && d.toISOString().slice(0, 10) === iso ? iso : null
}

/**
 * Date entry shown as dd.mm.yyyy regardless of the browser language. A native
 * date input would follow the browser locale (mm/dd/yyyy in an English
 * browser). The value passed in and out stays ISO; the calendar button opens
 * the browser's own picker through a hidden native input.
 */
export function DateField({ id, value, min, max, onChange }: {
  id?: string; value: string; min?: string; max?: string; onChange: (iso: string) => void
}) {
  const picker = useRef<HTMLInputElement>(null)
  const [text, setText] = useState(uaDate(value))
  useEffect(() => { setText(uaDate(value)) }, [value])

  const commit = () => {
    const iso = parseUaDate(text)
    const inRange = iso && (!min || iso >= min) && (!max || iso <= max)
    if (iso && inRange) onChange(iso)
    else setText(uaDate(value))
  }
  const open = () => {
    const el = picker.current
    if (!el) return
    try { el.showPicker() } catch { el.focus() }
  }

  return (
    <span className="date-field">
      <input
        id={id} type="text" inputMode="numeric" maxLength={10} placeholder="dd.mm.yyyy"
        value={text} onChange={e => setText(e.target.value)} onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit() }}
      />
      <button type="button" className="date-pick" aria-label="Open calendar" onClick={open}>
        <Icon.calendar />
      </button>
      <input
        ref={picker} type="date" className="date-native" tabIndex={-1} aria-hidden="true"
        value={value} min={min} max={max}
        onChange={e => { if (e.target.value) onChange(e.target.value) }}
      />
    </span>
  )
}

export function pct(v: number, digits = 1): string {
  return `${(v * 100).toFixed(digits)}%`
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7)
}

export function dateTime(iso: string | null): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('en-GB', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

/** "3 minutes ago" — short relative form used in tables. */
export function ago(iso: string | null): string {
  if (!iso) return '-'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 2592000) return `${Math.floor(s / 86400)}d ago`
  return dateTime(iso)
}

export function num(v: number): string {
  return v.toLocaleString('en-US')
}
