import { useEffect, useState } from 'react'
import { api, type FlakyTest } from '../lib/api'
import { Empty, ErrorBox, Loading, Notice, Tile, num, pct } from '../components/ui'

export default function FlakyTests({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<FlakyTest[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setRows(null); setError('')
    api.flaky(projectId).then(setRows).catch(e => setError(e.message))
  }, [projectId])

  if (error) return <ErrorBox message={error} />

  const quarantined = (rows ?? []).filter(r => r.quarantined).length
  const orderDependent = (rows ?? []).filter(r => r.order_dependent).length

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Flaky tests</h2>
        </div>
      </div>

      {!rows ? <Loading rows={6} /> : (
        <>
          <div className="grid k3" style={{ marginBottom: 16 }}>
            <Tile label="Candidates" value={num(rows.length)} hint="ranked by instability score" />
            <Tile label="Quarantined" value={num(quarantined)} tone={quarantined ? 'warn' : undefined}
                  hint="excluded from selection" />
            <Tile label="Order dependent" value={num(orderDependent)} tone={orderDependent ? 'warn' : undefined}
                  hint="must not be reordered" />
          </div>

          {orderDependent > 0 && (
            <Notice>
              Order-dependent tests keep their suite order and stay on one shard.
            </Notice>
          )}

          {rows.length === 0 ? (
            <Empty
              title="No flaky tests"
            />
          ) : (
            <div className="table-wrap">
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Suite</th><th>Test</th>
                      <th className="num">Instability</th>
                      <th className="num">Fail rate</th>
                      <th className="num">Runs</th><th>State</th><th>Ordering</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(t => (
                      <tr key={t.id}>
                        <td className="mono">{t.suite}</td>
                        <td className="mono">{t.name}</td>
                        <td className="num">
                          <div className="row" style={{ gap: 8, justifyContent: 'flex-end', flexWrap: 'nowrap' }}>
                            <span>{t.flaky_score.toFixed(3)}</span>
                            <div className={`bar${t.flaky_score > 0.2 ? ' warn' : ''}`}
                                 style={{ width: 62, marginTop: 0, flexShrink: 0 }}>
                              <span style={{ width: `${Math.min(100, t.flaky_score * 100)}%` }} />
                            </div>
                          </div>
                        </td>
                        <td className="num">{pct(t.fail_rate)}</td>
                        <td className="num">{num(t.runs)}</td>
                        <td>{t.quarantined
                          ? <span className="badge warn">Quarantined</span>
                          : <span className="muted">active</span>}</td>
                        <td>{t.order_dependent
                          ? <span className="badge warn" title={t.flaky_category || 'order dependent'}>order-dependent</span>
                          : <span className="muted">-</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="table-foot">
                <span>{num(rows.length)} candidates</span>
                <span>{num(quarantined)} quarantined · {num(orderDependent)} order dependent</span>
              </div>
            </div>
          )}
        </>
      )}
    </>
  )
}
