import { useEffect, useState } from 'react'
import { api, auth, type PeriodReport } from '../lib/api'
import {
  DateField, Empty, ErrorBox, Icon, Notice, SectionHead, Tile, TileSkeleton, dur, num, pct,
  uaDate,
} from '../components/ui'

const iso = (d: Date) => d.toISOString().slice(0, 10)
const daysAgo = (n: number) => { const d = new Date(); d.setUTCDate(d.getUTCDate() - n); return d }

/** Under an hour the saving is shown in minutes and seconds, not as "0.0 h". */
const savedText = (s: number) => s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s > 0 ? dur(s) : '0'

const PRESETS = [
  { label: 'Last 7 days', days: 7 },
  { label: 'Last 30 days', days: 30 },
  { label: 'Last 90 days', days: 90 },
]

/**
 * Звіт за довільний період.
 *
 * Дашборд відповідає на питання «що відбувається зараз» і рахує від
 * поточного моменту назад. Тут межі задає людина: спринт, місяць, квартал.
 * Той самий звіт можна зберегти таблицею або розіслати каналами сповіщень.
 */
export default function Reports({ projectId }: { projectId: number }) {
  const [from, setFrom] = useState(iso(daysAgo(29)))
  const [to, setTo] = useState(iso(new Date()))
  const [data, setData] = useState<PeriodReport | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState('')

  // Проєкт, який давно не працював, у типовому періоді порожній. Тому перший
  // показ прив'язується до останнього прогону, а не до сьогоднішнього дня.
  useEffect(() => {
    let alive = true
    api.dashboard(projectId, 1).then(d => {
      if (!alive || !d.last_run_at) return
      const last = new Date(d.last_run_at)
      if (last < daysAgo(29)) {
        const start = new Date(last); start.setUTCDate(start.getUTCDate() - 29)
        setFrom(iso(start)); setTo(iso(last))
      }
    }).catch(() => { /* the report request below reports the failure */ })
    return () => { alive = false }
  }, [projectId])

  useEffect(() => {
    if (from > to) { setError('The period starts after it ends.'); setData(null); return }
    let alive = true
    setData(null); setError(''); setNote('')
    api.report(projectId, from, to)
      .then(r => { if (alive) setData(r) })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [projectId, from, to])

  async function download() {
    setBusy('csv'); setNote('')
    try {
      const blob = await api.reportCsv(projectId, from, to)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `slipstream-${data?.project.name ?? projectId}-${from}-${to}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Download failed')
    } finally { setBusy('') }
  }

  async function send() {
    setBusy('send'); setNote('')
    try {
      const r = await api.sendDigest(projectId, from, to)
      const ok = Object.entries(r.channels).filter(([, v]) => v).map(([k]) => k)
      const failed = Object.entries(r.channels).filter(([, v]) => !v).map(([k]) => k)
      setNote(`Digest sent through: ${ok.join(', ') || 'no channel'}`
        + (failed.length ? `. Failed: ${failed.join(', ')}` : '.'))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy('') }
  }

  const p = data?.pipelines
  const o = data?.optimisation
  const d = data?.dora
  const q = data?.quality

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Reports</h2>
          <div className="sub">
            Both dates are included.
          </div>
        </div>
      </div>

      <div className="card period-bar">
        <div className="field">
          <label htmlFor="rep-from">From</label>
          <DateField id="rep-from" value={from} max={to} onChange={setFrom} />
        </div>
        <div className="field">
          <label htmlFor="rep-to">To</label>
          <DateField id="rep-to" value={to} min={from} onChange={setTo} />
        </div>
        <div className="seg">
          {PRESETS.map(x => (
            <button key={x.days} onClick={() => { setFrom(iso(daysAgo(x.days - 1))); setTo(iso(new Date())) }}>
              {x.label}
            </button>
          ))}
        </div>
        <div className="spacer" />
        {auth.canSendDigest && (
          <button className="btn ghost" disabled={!data || busy !== ''} onClick={send}
                  title="Send this summary through the configured notification channels">
            <Icon.mail />{busy === 'send' ? 'Sending…' : 'Send digest'}
          </button>
        )}
        <button className="btn" disabled={!data || busy !== ''} onClick={download}>
          <Icon.download />{busy === 'csv' ? 'Preparing…' : 'Download CSV'}
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {note && <Notice plain>{note}</Notice>}

      {!error && (!data || !p || !o || !d || !q ? <TileSkeleton /> : p.total === 0 ? (
        <Empty
          title="No runs in this period"
          text="The project has no pipeline runs between these dates."
        />
      ) : (
        <>
          <div className="grid k4" style={{ marginBottom: 20 }}>
            <Tile label="Pipeline runs" value={num(p.total)}
                  hint={`${num(p.success)} passed · ${num(p.failed)} failed`} />
            <Tile label="Success rate" value={pct(p.success_rate)}
                  tone={p.success_rate > 0.85 ? 'ok' : p.success_rate > 0.65 ? 'warn' : 'err'}
                  hint={`over ${data.period.days} days`} />
            <Tile label="Mean duration" value={dur(p.mean_duration_s)}
                  hint={`p50 ${dur(p.median_duration_s)} · p95 ${dur(p.p95_duration_s)}`} />
            <Tile label="Time saved" value={savedText(o.saved_seconds ?? o.saved_hours * 3600)}
                  hint={o.optimized_runs > 0
                    ? `${num(o.optimized_runs)} optimised runs · ${pct(o.optimized_share)} of all`
                    : o.planned_share > 0
                      ? `nothing applied yet · plans promise ${pct(o.potential_saving)}`
                      : 'no plan was applied in this period'} />
          </div>

          <SectionHead title="DORA metrics" />
          <div className="grid k4" style={{ marginBottom: 20 }}>
            <Tile label="Deployment frequency" value={d.deployment_frequency_per_day.toFixed(2)}
                  hint={`per day · ${num(d.deployments)} total`} />
            <Tile label="Lead time for changes" value={`${d.lead_time_hours.toFixed(1)}h`}
                  hint="commit to deployment" />
            <Tile label="Change failure rate" value={pct(d.change_failure_rate)}
                  tone={d.change_failure_rate < 0.15 ? 'ok' : d.change_failure_rate < 0.3 ? 'warn' : 'err'}
                  hint="share of deployments that failed" />
            <Tile label="Time to restore" value={`${d.time_to_restore_hours.toFixed(1)}h`}
                  hint="median to next green build" />
          </div>

          <SectionHead title="Attention" />
          <div className="grid k4" style={{ marginBottom: 20 }}>
            <Tile label="Alerts raised" value={num(q.alerts_raised)}
                  hint={`${num(q.alerts_open)} still open`} />
            <Tile label="Recommendations applied" value={num(q.recommendations_applied)}
                  hint={`${num(q.recommendations_rejected)} dismissed`} />
            <Tile label="Tests in quarantine" value={num(q.tests_quarantined)} hint="as of now" />
            <Tile label="Queue time" value={p.mean_queue_s ? dur(p.mean_queue_s) : '-'} hint="mean wait for a runner" />
          </div>

          <SectionHead title="Day by day"
                       right={<span className="badge neutral sm">{num(data.daily.length)} days with runs</span>} />
          <div className="table-wrap">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Date</th><th className="num">Runs</th><th className="num">Passed</th>
                    <th className="num">Failed</th><th className="num">Success rate</th>
                    <th className="num">Mean duration</th><th className="num">Optimised</th>
                  </tr>
                </thead>
                <tbody>
                  {data.daily.map(day => (
                    <tr key={day.date}>
                      <td className="mono">{uaDate(day.date)}</td>
                      <td className="num">{num(day.count)}</td>
                      <td className="num">{num(day.success)}</td>
                      <td className="num">{num(day.failed)}</td>
                      <td className="num">{pct(day.success_rate, 0)}</td>
                      <td className="num">{dur(day.mean_duration_s)}</td>
                      <td className="num">{num(day.optimized)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ))}
    </>
  )
}
