import { useEffect, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import { api, type Dashboard as D } from '../lib/api'
import {
  ErrorBox, ModeBadge, Notice, SectionHead, Tile, TileSkeleton, dur, num, pct,
} from '../components/ui'

/* Кольори графіків задано окремо від стилів: бібліотека побудови діаграм
   малює у svg і до оформлення сторінки не має доступу. Значення ті самі,
   що й у таблиці стилів, щоб панель лишалася однією системою. */
const C = {
  grid: '#e6e9ec',
  axis: '#697280',
  /* Основний ряд - акцентний синій; другий - сірий, щоб два ряди
     розрізнялися і в чорно-білому друку записки. */
  acc: '#2f5d9e',
  second: '#8a93a0',
}

const TOOLTIP = {
  contentStyle: {
    background: '#ffffff', border: '1px solid #d6dadf', borderRadius: 5,
    fontSize: 12, boxShadow: '0 1px 3px rgba(27,31,36,.12)',
  },
  labelStyle: { color: '#697280', marginBottom: 4 },
  itemStyle: { color: '#1b1f24' },
}

const RANGES = [
  { d: 7, label: '7d' }, { d: 30, label: '30d' },
  { d: 90, label: '90d' }, { d: 365, label: '1y' },
]

export default function Dashboard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<D | null>(null)
  const [days, setDays] = useState(30)
  const [error, setError] = useState('')

  // Вікно, обране людиною, не чіпаємо. А от типове вікно для проєкту, який
  // давно не працював, показало б самі нулі - тому один раз на проєкт воно
  // мовчки розширюється до найкоротшого, у яке потрапляє останній прогін:
  // обраний період і так видно на перемикачі.
  const [picked, setPicked] = useState(false)

  useEffect(() => { setPicked(false); setDays(30) }, [projectId])

  useEffect(() => {
    let alive = true
    setData(null); setError('')
    api.dashboard(projectId, days)
      .then(d => {
        if (!alive) return
        if (!picked && d.pipelines.total === 0 && d.suggested_days) {
          setDays(d.suggested_days)
          return
        }
        setData(d)
      })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [projectId, days])

  if (error) return <ErrorBox message={error} />

  const p = data?.pipelines
  const d = data?.dora
  const trend = (data?.trend ?? []).map(t => ({
    ...t,
    date: t.date.slice(5),
    mean_min: Number((t.mean_duration_s / 60).toFixed(1)),
    success_pct: Number((t.success_rate * 100).toFixed(0)),
  }))

  return (
    <>
      <div className="page-head">
        <div>
          <h2>{data?.project.name ?? 'Overview'}</h2>
          <div className="sub row" style={{ gap: 7 }}>
            <span>Optimisation mode</span>
            {data && <ModeBadge mode={data.project.mode} />}
          </div>
        </div>
        <div className="seg">
          {RANGES.map(r => (
            <button key={r.d} className={days === r.d ? 'on' : ''}
                    onClick={() => { setPicked(true); setDays(r.d) }}>
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {data && p && p.total === 0 && data.last_run_at && (
        <Notice plain>
          No runs in the last {days} days. Last run: {data.last_run_at.slice(0, 10)}.
        </Notice>
      )}

      {!data || !p || !d ? <TileSkeleton /> : (
        <>
          <div className="grid k4" style={{ marginBottom: 20 }}>
            <Tile
              label="Pipeline runs" value={num(p.total)}
              hint={`${num(p.success)} passed · ${num(p.failed)} failed`}
            />
            <Tile
              label="Success rate" value={pct(p.success_rate)}
              tone={p.success_rate > 0.85 ? 'ok' : p.success_rate > 0.65 ? 'warn' : 'err'}
              hint={`across ${days} days`}
            />
            <Tile
              label="Mean duration" value={dur(p.mean_duration_s)}
              hint={`p50 ${dur(p.median_duration_s)} · queue ${dur(p.mean_queue_s)}`}
            />
            {/* У холостому ході частка оптимізованих завжди нульова за
                визначенням: план рахується, але не застосовується. Показувати
                там голий нуль означає виглядати так, ніби система не працює.
                Корисне число для цього режиму - обіцяна економія. */}
            {data.project.mode === 'active' ? (
              <Tile
                label="Optimised runs" value={pct(p.optimized_share)} tone="acc" hero
                hint={`${num(Math.round(p.optimized_share * p.total))} of ${num(p.total)} runs used a plan`}
              />
            ) : (
              <Tile
                label="Potential saving" value={pct(p.potential_saving)} tone="acc" hero
                hint={p.planned_share > 0
                  ? `planned for ${pct(p.planned_share, 0)} of runs · not applied in ${data.project.mode.replace('_', ' ')}`
                  : 'no plans computed yet'}
              />
            )}
          </div>

          <SectionHead
            title="DORA metrics"
          />
          <div className="grid k4" style={{ marginBottom: 20 }}>
            <Tile
              label="Deployment frequency" value={d.deployment_frequency_per_day.toFixed(2)}
              hint={`per day · ${num(d.deployments)} total`}
            />
            <Tile
              label="Lead time for changes" value={`${d.lead_time_hours.toFixed(1)}h`}
              hint="commit to deployment"
            />
            <Tile
              label="Change failure rate" value={pct(d.change_failure_rate)}
              tone={d.change_failure_rate < 0.15 ? 'ok' : d.change_failure_rate < 0.3 ? 'warn' : 'err'}
              hint="share of deployments that failed"
            />
            <Tile
              label="Time to restore" value={`${d.time_to_restore_hours.toFixed(1)}h`}
              hint="median to next green build"
            />
          </div>

          <div className="grid k2">
            <div className="card">
              <div className="card-head">
                <h3>Mean pipeline duration, min</h3>
              </div>
              {trend.length > 1 ? (
                <ResponsiveContainer width="100%" height={196}>
                  <AreaChart data={trend} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                    <CartesianGrid stroke={C.grid} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" stroke={C.axis} fontSize={10.5} tickLine={false} axisLine={false} />
                    <YAxis stroke={C.axis} fontSize={10.5} tickLine={false} axisLine={false} />
                    <Tooltip {...TOOLTIP} />
                    <Area
                      type="linear" dataKey="mean_min" stroke={C.acc} strokeWidth={1.5}
                      fill={C.acc} fillOpacity={0.08} name="minutes" isAnimationActive={false}
                      dot={false} activeDot={{ r: 3, fill: C.acc, stroke: '#ffffff', strokeWidth: 2 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : <div className="empty"><p>Not enough data for a trend</p></div>}
            </div>

            <div className="card">
              <div className="card-head">
                <h3>Daily success rate, %</h3>
              </div>
              {trend.length > 1 ? (
                <ResponsiveContainer width="100%" height={196}>
                  <BarChart data={trend} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                    <CartesianGrid stroke={C.grid} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" stroke={C.axis} fontSize={10.5} tickLine={false} axisLine={false} />
                    <YAxis stroke={C.axis} fontSize={10.5} domain={[0, 100]} tickLine={false} axisLine={false} />
                    <Tooltip {...TOOLTIP} cursor={{ fill: 'rgba(47,93,158,.06)' }} />
                    <Bar dataKey="success_pct" fill={C.acc} name="percent" isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              ) : <div className="empty"><p>Not enough data for a trend</p></div>}
            </div>
          </div>

          <div className="grid k2" style={{ marginTop: 14 }}>
            <div className="card pad-0">
              <div className="card-head" style={{ padding: '15px 16px 0', marginBottom: 11 }}>
                <h3>Slowest jobs</h3>
              </div>
              {data.slowest_jobs.length ? (
                <div className="table-scroll">
                  <table style={{ minWidth: 0 }}>
                    <thead><tr><th>Job</th><th className="num">Mean</th><th className="num">Runs</th></tr></thead>
                    <tbody>
                      {data.slowest_jobs.map(j => (
                        <tr key={j.name}>
                          <td className="mono">{j.name}</td>
                          <td className="num">{dur(j.mean_duration_s)}</td>
                          <td className="num">{num(j.runs)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <div className="empty"><p>No job data yet</p></div>}
            </div>

            <div className="card pad-0">
              <div className="card-head" style={{ padding: '15px 16px 0', marginBottom: 11 }}>
                <h3>Most unstable tests</h3>
              </div>
              {data.flakiest_tests.length ? (
                <div className="table-scroll">
                  <table style={{ minWidth: 0 }}>
                    <thead><tr><th>Test</th><th className="num">Score</th><th>State</th></tr></thead>
                    <tbody>
                      {data.flakiest_tests.map(t => (
                        <tr key={t.id}>
                          <td className="mono">{t.suite}/{t.name}</td>
                          <td className="num">{t.flaky_score.toFixed(3)}</td>
                          <td>{t.quarantined
                            ? <span className="badge warn">Quarantined</span>
                            : <span className="badge neutral">Active</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <div className="empty"><p>No unstable tests detected</p></div>}
            </div>
          </div>
        </>
      )}
    </>
  )
}
