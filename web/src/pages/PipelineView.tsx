import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { api, type Plan, type PipelineDetail, type PredictionRow } from '../lib/api'
import {
  ErrorBox, Loading, ModeBadge, Notice, StatusBadge, Tile,
  dateTime, dur, num, pct, shortSha,
} from '../components/ui'
import ReportProblem from '../components/ReportProblem'

/** The API labels predictions in the project language; the console is English,
 *  so labels are resolved from the stable `type` field instead. */
const PRED_LABEL: Record<string, string> = {
  failure: 'Failure risk',
  duration: 'Pipeline duration',
  test_rank: 'Test ranking',
  anomaly: 'Anomaly score',
}

function fmtPrediction(p: PredictionRow, v: number): string {
  if (p.type === 'failure') return pct(v)
  if (p.unit === 'с' || p.unit === 's') return dur(v)
  return String(v)
}

/** How far the prediction was from reality, as a signed share. */
function accuracy(p: PredictionRow): { text: string; cls: string } | null {
  if (p.actual === null || p.actual === undefined) return null
  if (p.type === 'failure') {
    const diff = Math.abs(p.predicted - p.actual)
    return {
      text: `${(diff * 100).toFixed(0)} pp off`,
      cls: diff < 0.2 ? 'ok' : diff < 0.45 ? 'warn' : 'err',
    }
  }
  if (!p.actual) return null
  const rel = Math.abs(p.predicted - p.actual) / p.actual
  return {
    text: `${(rel * 100).toFixed(0)}% off`,
    cls: rel < 0.2 ? 'ok' : rel < 0.5 ? 'warn' : 'err',
  }
}

export default function PipelineView() {
  const { id } = useParams()
  // back to the same page of the list the run was opened from
  const back = (useLocation().state as { from?: string } | null)?.from ?? '/pipelines'
  const [data, setData] = useState<PipelineDetail | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [yaml, setYaml] = useState('')
  const [error, setError] = useState('')
  const [planError, setPlanError] = useState('')
  const [fromSnapshot, setFromSnapshot] = useState(false)

  useEffect(() => {
    if (!id) return
    let alive = true
    api.pipeline(Number(id))
      .then(d => {
        if (!alive) return
        setData(d)
        // Знімок плану - те рішення, яке система ухвалила ТОДІ. Перерахунок
        // заднім числом дав би інший результат, бо історія відтоді змінилася,
        // тому він лишається запасним шляхом для старих прогонів.
        const snap = d.plan_snapshot as unknown as Plan | undefined
        if (snap && typeof snap.est_duration_s === 'number') {
          setPlan(snap)
          setFromSnapshot(true)
          return undefined
        }
        // The project comes from the run itself: the sidebar may point at a
        // different project, and the plan would then be built for the wrong repo.
        return api.previewPlan(d.project_id, d.commit_sha)
      })
      .then(r => { if (alive && r) { setPlan(r.plan); setYaml(r.gitlab_yaml) } })
      .catch(e => {
        if (!alive) return
        setData(prev => { if (!prev) setError(e.message); else setPlanError(e.message); return prev })
      })
    return () => { alive = false }
  }, [id])

  if (error) return <ErrorBox message={error} />
  if (!data) return <Loading rows={6} />

  return (
    <>
      <div className="page-head">
        <div>
          <h2 className="row" style={{ gap: 10 }}>
            Run #{data.id}
            <StatusBadge status={data.status} />
            {data.optimized && <span className="badge accent">Optimised</span>}
          </h2>
          <div className="sub mono" style={{ fontSize: 12 }}>
            {shortSha(data.commit_sha)} · {data.ref} · {data.commit_author || 'unknown author'}
            {data.linked_run_id && data.linked_role === 'plan' && (
              <> · plan issued in <Link to={`/pipelines/${data.linked_run_id}`}>run #{data.linked_run_id}</Link></>
            )}
            {data.linked_run_id && data.linked_role === 'execution' && (
              <> · checks ran in <Link to={`/pipelines/${data.linked_run_id}`}>run #{data.linked_run_id}</Link></>
            )}
          </div>
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          <ReportProblem
            projectId={data.project_id} subject="pipeline" subjectId={data.id}
            label="Report a problem with this plan"
          />
          <Link to={back} className="btn ghost">Back to runs</Link>
        </div>
      </div>

      {data.commit_message && (
        <Notice plain>
          <span className="mono" style={{ fontSize: 12.5 }}>{data.commit_message.split('\n')[0]}</span>
        </Notice>
      )}

      <div className="grid k4" style={{ marginBottom: 16 }}>
        <Tile label="Duration" value={dur(data.duration_s)} />
        <Tile label="Queue time" value={data.queue_time_s ? dur(data.queue_time_s) : '-'} />
        <Tile label="Files changed" value={data.files_changed ? num(data.files_changed) : '-'} />
        <Tile label="Started" value={<span style={{ fontSize: 17 }}>{dateTime(data.started_at)}</span>} />
      </div>

      <div className="grid k2">
        <div className="stack">
          <div className="card pad-0">
            <div className="card-head" style={{ padding: '15px 16px 0', marginBottom: 11 }}>
              <h3>Jobs</h3>
              <span className="badge neutral sm">{data.jobs.length}</span>
            </div>
            {data.jobs.length ? (
              <div className="table-scroll">
                <table style={{ minWidth: 0 }}>
                  <thead><tr><th>Name</th><th>Status</th><th className="num">Duration</th></tr></thead>
                  <tbody>
                    {data.jobs.map(j => (
                      <tr key={j.id}>
                        <td className="mono">{j.name}</td>
                        <td><StatusBadge status={j.status} /></td>
                        <td className="num">{dur(j.duration_s)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className="empty"><p>No job data for this run</p></div>}
          </div>

          {data.predictions?.length > 0 && (
            <div className="card pad-0">
              <div className="card-head" style={{ padding: '15px 16px 0', marginBottom: 11 }}>
                <h3>Prediction vs reality</h3>
              </div>
              <div className="table-scroll">
                <table style={{ minWidth: 0 }}>
                  <thead>
                    <tr>
                      <th>Signal</th><th className="num">Predicted</th>
                      <th className="num">Actual</th><th className="num">Error</th><th className="num">Model</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.predictions.map(p => {
                      const acc = accuracy(p)
                      return (
                        <tr key={p.type}>
                          <td>{PRED_LABEL[p.type] ?? p.label}</td>
                          <td className="num">{fmtPrediction(p, p.predicted)}</td>
                          <td className="num">{p.actual === null ? '-' : fmtPrediction(p, p.actual)}</td>
                          <td className="num">
                            {acc ? <span className={`badge ${acc.cls} sm`}>{acc.text}</span> : '-'}
                          </td>
                          <td className="num mono">{p.model_version ?? '-'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h3>Plan for this commit</h3>
            <div className="row" style={{ gap: 6 }}>
              <span className="badge neutral sm" title={fromSnapshot
                ? 'The plan the system actually issued for this run'
                : 'No stored plan for this run: recomputed against today’s history'}>
                {fromSnapshot ? (data.plan_from_run ? `as issued in #${data.plan_from_run}` : 'as issued') : 'recomputed'}
              </span>
              {plan && <ModeBadge mode={plan.mode} />}
            </div>
          </div>

          {planError && <ErrorBox message={planError} />}
          {!plan && !planError && (
            <>
              <div className="skeleton sk-row" style={{ width: '70%' }} />
              <div className="skeleton sk-row" style={{ width: '46%' }} />
            </>
          )}

          {plan && (
            <>
              <div className="row" style={{ gap: 6, marginBottom: 14 }}>
                <span className="badge neutral">Risk {pct(plan.risk)}</span>
                <span className="badge neutral">{num(plan.selected_tests)} / {num(plan.total_tests)} tests</span>
                <span className="badge neutral">{plan.shards} shard{plan.shards === 1 ? '' : 's'}</span>
              </div>

              <div style={{ fontSize: 13, color: 'var(--ink-2)' }}>
                Estimated <b style={{ color: 'var(--ink)' }}>{dur(plan.est_duration_s)}</b>
                {' '}instead of {dur(plan.baseline_duration_s)}
              </div>
              <div className="bar">
                <span style={{ width: `${Math.max(0, Math.min(100, (1 - plan.saving_ratio) * 100))}%` }} />
              </div>
              <div style={{ fontSize: 12, color: 'var(--acc)', marginTop: 6, fontWeight: 500 }}>
                {pct(plan.saving_ratio)} saved
              </div>

              {plan.rationale.length > 0 && (
                <>
                  <div className="side-label" style={{ padding: '16px 0 5px' }}>Why</div>
                  <ul className="reason-list">{plan.rationale.map((r, i) => <li key={i}>{r}</li>)}</ul>
                </>
              )}
              {plan.fallbacks.length > 0 && (
                <>
                  <div className="side-label" style={{ padding: '14px 0 5px' }}>Safeguards triggered</div>
                  <ul className="reason-list warn">{plan.fallbacks.map((r, i) => <li key={i}>{r}</li>)}</ul>
                </>
              )}
            </>
          )}
        </div>
      </div>

      {data.tests_run > 0 ? (
        <div className="card pad-0" style={{ marginTop: 14 }}>
          <div className="card-head" style={{ padding: '15px 16px 0', marginBottom: 6 }}>
            <h3>Checks in this run</h3>
            <span style={{ fontSize: 12.5, color: 'var(--ink-2)' }}>
              {num(data.tests_run)} of {num(data.tests_total)} ran
              {data.tests_total > data.tests_run && ` · ${num(data.tests_total - data.tests_run)} not run`}
              {data.tests_failed > 0 && <span style={{ color: 'var(--err)' }}> · {num(data.tests_failed)} failed</span>}
            </span>
          </div>
          {data.changed_paths.length > 0 && (
            <div className="checks-note">
              Changed: <span className="mono">{data.changed_paths.slice(0, 6).join(', ')}
              {data.changed_paths.length > 6 && ` +${data.changed_paths.length - 6}`}</span>
            </div>
          )}
          <div className="table-scroll">
            <table style={{ minWidth: 0 }}>
              <thead>
                <tr>
                  <th>Suite</th><th className="num">Ran</th><th className="num">Not run</th>
                  <th className="num">Failed</th><th style={{ width: '38%' }} />
                </tr>
              </thead>
              <tbody>
                {data.checks_by_suite.map(r => {
                  const all = r.ran + r.not_run || 1
                  return (
                    <tr key={r.suite}>
                      <td className="mono">{r.suite}</td>
                      <td className="num">{r.ran || '-'}</td>
                      <td className="num" style={{ color: 'var(--muted)' }}>{r.not_run || '-'}</td>
                      <td className="num">{r.failed ? <span style={{ color: 'var(--err)' }}>{r.failed}</span> : '-'}</td>
                      <td>
                        <div className="split-bar" title={`${r.ran} ran, ${r.not_run} not run`}>
                          <span className="ran" style={{ width: `${(r.ran / all) * 100}%` }} />
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {data.failed_checks.length > 0 && (
            <div className="checks-note" style={{ paddingTop: 10 }}>
              Failed: <span className="mono">{data.failed_checks.join(', ')}</span>
            </div>
          )}
        </div>
      ) : data.linked_role === 'execution' && data.linked_run_id ? (
        <div style={{ marginTop: 14 }}>
          <Notice plain>
            This run only requested the plan. GitLab executed it as a child pipeline:{' '}
            <Link to={`/pipelines/${data.linked_run_id}`}>run #{data.linked_run_id}</Link>.
          </Notice>
        </div>
      ) : null}

      {yaml && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="card-head">
            <h3>Generated child pipeline</h3>
            <button
              className="btn ghost sm"
              onClick={() => navigator.clipboard?.writeText(yaml)}
            >
              Copy YAML
            </button>
          </div>
          <pre className="code">{yaml}</pre>
        </div>
      )}
    </>
  )
}
