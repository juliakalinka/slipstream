import { useEffect, useState } from 'react'
import { api, auth, type ModelVersion } from '../lib/api'
import {
  Empty, ErrorBox, Icon, Loading, Notice, SectionHead, dateTime, num,
} from '../components/ui'

const TYPE_LABEL: Record<string, string> = {
  failure: 'Build failure risk',
  duration: 'Job duration',
  test_rank: 'Test ranking',
  anomaly: 'Anomaly detection',
}

const METRIC_LABEL: Record<string, string> = {
  roc_auc: 'ROC-AUC', pr_auc: 'PR-AUC', precision: 'Precision', recall: 'Recall',
  f1: 'F1', brier: 'Brier score', threshold: 'Decision threshold', base_rate: 'Base rate',
  mae_s: 'MAE (s)', mape: 'MAPE', r2: 'R²', mae_improvement: 'Gain over baseline',
  baseline_mae_s: 'Baseline MAE (s)', flagged_ratio: 'Flagged share',
  lift_over_random: 'Lift over random', train_samples: 'Training samples',
  test_samples: 'Holdout samples', positives: 'Positive samples', flagged: 'Flagged',
}

/** Metrics where a higher value is better and a low one deserves a warning. */
const QUALITY: Record<string, { warn: number; good: number }> = {
  roc_auc: { warn: 0.6, good: 0.75 },
  r2: { warn: 0.3, good: 0.6 },
  lift_over_random: { warn: 1.5, good: 3 },
}

function fmt(v: unknown): string {
  if (typeof v === 'number') return Number.isInteger(v) ? num(v) : v.toFixed(4)
  return String(v)
}

function tone(key: string, v: unknown): string {
  const q = QUALITY[key]
  if (!q || typeof v !== 'number') return ''
  return v >= q.good ? 'ok' : v >= q.warn ? 'warn' : 'err'
}

export default function Models({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<ModelVersion[] | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [acting, setActing] = useState(0)

  function load() {
    setRows(null); setError('')
    api.models(projectId).then(setRows).catch(e => setError(e.message))
  }
  useEffect(load, [projectId])

  async function retrain() {
    setBusy(true); setNote('')
    try {
      await api.retrain(projectId)
      setNote('Retraining started in the background. Refresh in a few minutes to see the new version.')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(false) }
  }

  /** Відкат на попередню версію - штатна операція, а не аварійна. */
  async function activate(modelId: number) {
    setActing(modelId); setNote('')
    try {
      await api.activateModel(modelId)
      setNote('Version activated. New plans will use it from now on.')
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setActing(0) }
  }

  if (error) return <ErrorBox message={error} />

  const active = (rows ?? []).filter(m => m.is_active)
  const history = (rows ?? []).filter(m => !m.is_active)

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Models</h2>
          <div className="sub">
            {rows ? `${active.length} active · ${rows.length} versions in the registry`
                  : ''}
          </div>
        </div>
        {auth.isDevops && (
          <button className="btn" onClick={retrain} disabled={busy}>
            {busy ? 'Starting…' : <><Icon.refresh />Retrain</>}
          </button>
        )}
      </div>

      {note && <Notice>{note}</Notice>}

      {!rows ? <Loading rows={6} /> : active.length === 0 ? (
        <Empty
          title="No trained models yet"
          text="Collect pipeline history first, then start a retraining run."
        />
      ) : (
        <div className="grid k2">
          {active.map(m => (
            <div className="card" key={m.id}>
              <div className="card-head">
                <h3 style={{ color: 'var(--ink)', fontSize: 14.5 }}>{TYPE_LABEL[m.type] ?? m.type}</h3>
                <span className="badge accent mono sm">{m.version}</span>
              </div>
              <div className="mono" style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 12 }}>
                {m.algorithm} · {dateTime(m.trained_at)} · {num(m.train_samples)} samples
              </div>

              <div className="table-scroll">
                <table style={{ minWidth: 0 }}>
                  <tbody>
                    {Object.entries(m.metrics)
                      .filter(([k]) => k !== 'feature_importance')
                      .map(([k, v]) => {
                        const t = tone(k, v)
                        return (
                          <tr key={k}>
                            <td style={{ fontSize: 12.5, color: 'var(--ink-2)' }}>{METRIC_LABEL[k] ?? k}</td>
                            <td className="num">
                              {t ? <span className={`badge ${t} sm mono`}>{fmt(v)}</span> : fmt(v)}
                            </td>
                          </tr>
                        )
                      })}
                  </tbody>
                </table>
              </div>

              {'feature_importance' in m.metrics && (
                <div style={{ marginTop: 12, paddingTop: 11, borderTop: '1px solid var(--line-soft)' }}>
                  <div className="side-label" style={{ padding: '0 0 8px' }}>Top features</div>
                  {Object.entries(m.metrics.feature_importance as Record<string, number>)
                    .slice(0, 5)
                    .map(([k, v]) => (
                      <div key={k} className="row" style={{ gap: 9, marginBottom: 5, flexWrap: 'nowrap' }}>
                        <span className="mono" style={{ fontSize: 11, color: 'var(--ink-2)', width: 158, flexShrink: 0 }}>{k}</span>
                        <div className="bar" style={{ flex: 1, marginTop: 0 }}>
                          <span style={{ width: `${Math.min(100, v * 420)}%` }} />
                        </div>
                        <span className="mono" style={{ fontSize: 10.5, color: 'var(--muted)', width: 40, textAlign: 'right' }}>
                          {v.toFixed(3)}
                        </span>
                      </div>
                    ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {history.length > 0 && (
        <>
          <SectionHead title="Version history" right={<span className="badge neutral sm">{num(history.length)}</span>} />
          <div className="table-wrap">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Model</th><th>Version</th><th>Algorithm</th>
                    <th className="num">Samples</th><th>Trained</th><th />
                  </tr>
                </thead>
                <tbody>
                  {history.map(m => (
                    <tr key={m.id}>
                      <td>{TYPE_LABEL[m.type] ?? m.type}</td>
                      <td className="mono">{m.version}</td>
                      <td className="mono">{m.algorithm}</td>
                      <td className="num">{num(m.train_samples)}</td>
                      <td className="mono">{dateTime(m.trained_at)}</td>
                      <td style={{ textAlign: 'right' }}>
                        {auth.isDevops && (
                          <button
                            className="btn ghost sm" disabled={acting !== 0}
                            onClick={() => activate(m.id)}
                            title="Make this version active again"
                          >
                            {acting === m.id ? 'Activating…' : 'Activate'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  )
}
