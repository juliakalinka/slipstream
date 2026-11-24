import { useEffect, useState } from 'react'
import { api, auth, type Recommendation } from '../lib/api'
import {
  Empty, ErrorBox, Icon, Loading, Notice, SectionHead, dateTime, dur, num,
} from '../components/ui'

const TYPE_LABEL: Record<string, string> = {
  quarantine_flaky: 'Flaky test',
  increase_shards: 'Parallelism',
  reorder_jobs: 'Job order',
  enable_cache: 'Caching',
  split_slow_job: 'Slow job',
  reduce_test_scope: 'Test scope',
}

export default function Recommendations({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<Recommendation[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(0)
  const [note, setNote] = useState('')

  function load() {
    api.recommendations(projectId).then(setRows).catch(e => setError(e.message))
  }
  useEffect(load, [projectId])

  async function generate() {
    setBusy(-1); setNote('')
    try {
      const created = await api.generateRecos(projectId)
      setNote(created.length
        ? `${created.length} new recommendation${created.length === 1 ? '' : 's'} generated.`
        : 'Nothing new to suggest.')
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(0) }
  }

  async function act(id: number, apply: boolean) {
    setBusy(id); setNote('')
    try {
      const r = apply ? await api.applyReco(id) : await api.rejectReco(id)
      const applied = r.applied?.length ? r.applied.join('; ') : ''
      const warn = r.warnings?.length ? r.warnings.join('; ') : ''
      setNote(apply
        ? `Applied. ${applied}${warn ? ` Note: ${warn}` : ''}`
        : 'Recommendation dismissed.')
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(0) }
  }

  if (error) return <ErrorBox message={error} />

  const active = (rows ?? []).filter(r => r.status === 'new')
  const resolved = (rows ?? []).filter(r => r.status !== 'new')
  const totalGain = active.reduce((s, r) => s + r.expected_gain_s, 0)

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Recommendations</h2>
          <div className="sub">
            {!rows ? ''
              : active.length === 0 ? 'No open recommendations'
              : `${active.length} open, estimated gain ${dur(totalGain)}`}
          </div>
        </div>
        {auth.isDevops && (
          <button className="btn" onClick={generate} disabled={busy !== 0}>
            {busy === -1 ? 'Analysing…' : <><Icon.refresh />Analyse project</>}
          </button>
        )}
      </div>

      {note && <Notice>{note}</Notice>}
      {!auth.isDevops && (
        <Notice plain>
          Applying a recommendation requires the DevOps engineer role.
        </Notice>
      )}

      {!rows ? <Loading rows={5} /> : (
        <>
          {active.length === 0 && (
            <Empty
              title="No open recommendations"
              text="Analyse the project to look for new ones."
            />
          )}

          {active.map(r => (
            <div className="card" key={r.id} style={{ marginBottom: 11 }}>
              <div className="row" style={{ alignItems: 'flex-start', gap: 14 }}>
                <div style={{ flex: '1 1 340px', minWidth: 0 }}>
                  <div className="row" style={{ gap: 6, marginBottom: 7 }}>
                    <span className="badge accent">{TYPE_LABEL[r.type] ?? r.type}</span>
                    <span className="badge ok">saves ~{dur(r.expected_gain_s)}</span>
                  </div>
                  <h3 style={{ fontSize: 14.5, color: 'var(--ink)', marginBottom: 6 }}>{r.title}</h3>
                  <p style={{ margin: 0, fontSize: 13, color: 'var(--ink-2)' }}>{r.description}</p>
                </div>
                {auth.isDevops && (
                  <div className="row" style={{ gap: 7 }}>
                    <button className="btn sm" disabled={busy !== 0} onClick={() => act(r.id, true)}>
                      <Icon.check />Apply
                    </button>
                    <button className="btn ghost sm" disabled={busy !== 0} onClick={() => act(r.id, false)}>
                      Dismiss
                    </button>
                  </div>
                )}
              </div>
              {Object.keys(r.evidence ?? {}).length > 0 && (
                <div style={{
                  marginTop: 12, paddingTop: 11, borderTop: '1px solid var(--line-soft)',
                  fontSize: 11.5, color: 'var(--muted)', fontFamily: 'var(--mono)',
                  display: 'flex', flexWrap: 'wrap', gap: '4px 16px',
                }}>
                  {Object.entries(r.evidence).slice(0, 6).map(([k, v]) => (
                    <span key={k}>{k}: <span style={{ color: 'var(--ink-2)' }}>{String(v)}</span></span>
                  ))}
                </div>
              )}
            </div>
          ))}

          {resolved.length > 0 && (
            <>
              <SectionHead title="History" right={<span className="badge neutral sm">{num(resolved.length)}</span>} />
              <div className="table-wrap">
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Recommendation</th><th>Type</th><th>Outcome</th>
                        <th className="num">Estimated gain</th><th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {resolved.map(r => (
                        <tr key={r.id}>
                          <td>{r.title}</td>
                          <td className="mono">{TYPE_LABEL[r.type] ?? r.type}</td>
                          <td>{r.status === 'applied'
                            ? <span className="badge ok">Applied</span>
                            : <span className="badge neutral">Dismissed</span>}</td>
                          <td className="num">{dur(r.expected_gain_s)}</td>
                          <td className="mono">{dateTime(r.created_at)}</td>
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
