import { useEffect, useState } from 'react'
import { api, auth, type Alert, type ProblemReport } from '../lib/api'
import {
  Empty, ErrorBox, Icon, Loading, Notice, SectionHead, Tile, ago, num,
} from '../components/ui'
import ReportProblem from '../components/ReportProblem'

const KIND_LABEL: Record<string, string> = {
  duration_spike: 'Duration spike',
  queue_delay: 'Queue delay',
  failure_rate: 'Failure rate',
  flaky_burst: 'Flaky burst',
}

const SEVERITY: Record<string, string> = {
  critical: 'err', warning: 'warn', info: 'info',
}

/** Деталі аномалії - це числа, а не проза: показуємо їх як є, компактно. */
function Details({ details }: { details: Record<string, unknown> }) {
  const rows = Object.entries(details ?? {}).slice(0, 8)
  if (!rows.length) return null
  return (
    <div style={{
      marginTop: 11, paddingTop: 10, borderTop: '1px solid var(--line-soft)',
      display: 'flex', flexWrap: 'wrap', gap: '4px 16px',
      fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--muted)',
    }}>
      {rows.map(([k, v]) => (
        <span key={k}>
          {k}: <span style={{ color: 'var(--ink-2)' }}>
            {typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : String(v)}
          </span>
        </span>
      ))}
    </div>
  )
}

export default function Alerts({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<Alert[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(0)

  const [problems, setProblems] = useState<ProblemReport[]>([])

  const loadProblems = () => {
    api.problems(projectId).then(setProblems).catch(() => setProblems([]))
  }
  function load() {
    setRows(null); setError('')
    api.alerts(projectId).then(setRows).catch(e => setError(e.message))
    loadProblems()
  }
  useEffect(load, [projectId])

  async function resolve(id: number) {
    setBusy(-id)
    try {
      const done = await api.resolveProblem(projectId, id)
      setProblems(ps => ps.map(x => (x.id === id ? done : x)))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(0) }
  }

  async function ack(id: number) {
    setBusy(id)
    try {
      await api.ackAlert(projectId, id)
      setRows(rs => rs?.map(a => (a.id === id ? { ...a, acknowledged: true } : a)) ?? rs)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(0) }
  }

  if (error) return <ErrorBox message={error} />

  const open = (rows ?? []).filter(a => !a.acknowledged)
  const done = (rows ?? []).filter(a => a.acknowledged)
  const critical = open.filter(a => a.severity === 'critical').length

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Alerts</h2>
        </div>
      </div>

      {!rows ? <Loading rows={5} /> : (
        <>
          <div className="grid k3" style={{ marginBottom: 16 }}>
            <Tile label="Open" value={num(open.length)} tone={open.length ? 'warn' : undefined}
                  hero={open.length > 0} hint="waiting for review" />
            <Tile label="Needs attention" value={num(critical)} tone={critical ? 'err' : undefined}
                  hint="high or critical severity" />
            <Tile label="Acknowledged" value={num(done.length)} hint="kept for history" />
          </div>

          {!auth.isDevops && (
            <Notice plain>
              Acknowledging requires the DevOps engineer role. Anyone can mark an alert as a false alarm.
            </Notice>
          )}

          {open.length === 0 ? (
            <Empty
              title="No open alerts"
            />
          ) : open.map(a => (
            <div className="card" key={a.id} style={{ marginBottom: 11 }}>
              <div className="row" style={{ alignItems: 'flex-start', gap: 14 }}>
                <div style={{ flex: '1 1 340px', minWidth: 0 }}>
                  <div className="row" style={{ gap: 6, marginBottom: 7 }}>
                    <span className={`badge ${SEVERITY[a.severity] ?? 'neutral'}`}>{a.severity}</span>
                    <span className="badge neutral sm">{KIND_LABEL[a.kind] ?? a.kind}</span>
                    <span className="muted mono" style={{ fontSize: 11 }}>{ago(a.created_at)}</span>
                  </div>
                  <h3 style={{ fontSize: 14, color: 'var(--ink)', marginBottom: 5 }}>{a.message}</h3>
                </div>
                <div className="row" style={{ gap: 7, justifyContent: 'flex-end', flex: '0 1 auto' }}>
                  {/* Позначити хибну тривогу може будь-хто: її причину
                      найчастіше знає розробник, а не той, хто закриває сповіщення. */}
                  <ReportProblem
                    projectId={projectId} subject="alert" subjectId={a.id}
                    label="False alarm" onSent={loadProblems}
                  />
                  {auth.isDevops && (
                    <button className="btn ghost sm" disabled={busy !== 0} onClick={() => ack(a.id)}>
                      <Icon.check />Acknowledge
                    </button>
                  )}
                </div>
              </div>
              <Details details={a.details} />
            </div>
          ))}

          {problems.length > 0 && (
            <>
              <SectionHead
                title="Reported problems"
                right={<span className="badge neutral sm">
                  {num(problems.filter(x => !x.resolved).length)} open
                </span>}
              />
              <div className="table-wrap" style={{ marginBottom: 16 }}>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr><th>About</th><th>What is wrong</th><th>Reported by</th><th>When</th><th>State</th></tr>
                    </thead>
                    <tbody>
                      {problems.map(x => (
                        <tr key={x.id}>
                          <td className="mono">
                            {x.subject_type.replace('_', ' ')}{x.subject_id ? ` #${x.subject_id}` : ''}
                          </td>
                          <td>{x.note}</td>
                          <td className="mono">{x.reported_by.split('@')[0] || '-'}</td>
                          <td className="mono">{ago(x.created_at)}</td>
                          <td>
                            {x.resolved
                              ? <span className="badge neutral sm">Reviewed</span>
                              : auth.isDevops
                                ? <button className="btn ghost sm" disabled={busy !== 0}
                                          onClick={() => resolve(x.id)}>
                                    <Icon.check />Mark reviewed
                                  </button>
                                : <span className="badge warn sm">Open</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}

          {done.length > 0 && (
            <>
              <SectionHead title="Acknowledged" right={<span className="badge neutral sm">{num(done.length)}</span>} />
              <div className="table-wrap">
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr><th>Severity</th><th>Kind</th><th>Message</th><th>Raised</th></tr>
                    </thead>
                    <tbody>
                      {done.map(a => (
                        <tr key={a.id}>
                          <td><span className="badge neutral sm">{a.severity}</span></td>
                          <td className="mono">{KIND_LABEL[a.kind] ?? a.kind}</td>
                          <td>{a.message}</td>
                          <td className="mono">{ago(a.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </>
  )
}
