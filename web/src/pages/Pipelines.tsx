import { useEffect, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { api, type Pipeline, type PipelineQuery } from '../lib/api'
import {
  Empty, ErrorBox, Icon, Loading, StatusBadge, ago, dur, num, shortSha,
} from '../components/ui'

const LIMITS = [25, 50, 100, 200]
const DEFAULT_LIMIT = 50

type Only = 'all' | 'failed' | 'optimised'
const ONLY: Only[] = ['all', 'failed', 'optimised']
const LABEL: Record<Only, string> = { all: 'All', failed: 'Failed', optimised: 'Optimised' }
const FILTER: Record<Only, PipelineQuery> = {
  all: {}, failed: { status: 'failed' }, optimised: { optimized: true },
}

export default function Pipelines({ projectId }: { projectId: number }) {
  const nav = useNavigate()
  const loc = useLocation()
  // Filter, search and page live in the address, so going back from a run
  // returns to the same page of the list.
  const [params, setParams] = useSearchParams()
  const only = (ONLY.find(k => k === params.get('only')) ?? 'all') as Only
  const limit = LIMITS.find(l => l === Number(params.get('rows'))) ?? DEFAULT_LIMIT
  const page = Math.max(1, Math.floor(Number(params.get('page'))) || 1)
  const q = params.get('q') ?? ''

  const [text, setText] = useState(q)
  const [rows, setRows] = useState<Pipeline[] | null>(null)
  const [total, setTotal] = useState(0)
  const [counts, setCounts] = useState<Record<Only, number> | null>(null)
  const [error, setError] = useState('')

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === '') next.delete(k)
      else next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  useEffect(() => { setText(q) }, [q])

  useEffect(() => {
    if (text.trim() === q) return
    const t = setTimeout(() => update({ q: text.trim(), page: null }), 350)
    return () => clearTimeout(t)
  }, [text])

  useEffect(() => {
    let alive = true
    setRows(null); setError('')
    api.pipelines(projectId, { ...FILTER[only], q, limit, offset: (page - 1) * limit })
      .then(r => { if (alive) { setRows(r.rows); setTotal(r.total) } })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [projectId, only, q, limit, page])

  useEffect(() => {
    let alive = true
    setCounts(null)
    Promise.all(ONLY.map(k => api.pipelines(projectId, { ...FILTER[k], q, limit: 1 })))
      .then(([all, failed, optimised]) => {
        if (alive) setCounts({ all: all.total, failed: failed.total, optimised: optimised.total })
      })
      .catch(() => { /* the list itself reports errors */ })
    return () => { alive = false }
  }, [projectId, q])

  // A page past the end, e.g. after switching to a smaller project.
  useEffect(() => {
    if (rows && rows.length === 0 && total > 0 && page > 1) update({ page: null })
  }, [rows, total])

  const pages = Math.max(1, Math.ceil(total / limit))
  const first = total ? (page - 1) * limit + 1 : 0
  const last = Math.min(total, page * limit)
  const open = (id: number) =>
    nav(`/pipelines/${id}`, { state: { from: loc.pathname + loc.search } })

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Pipelines</h2>
        </div>
        <div className="head-actions">
          <input
            className="search" type="search" value={text} maxLength={64}
            placeholder="Run #, commit or branch"
            onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') update({ q: text.trim(), page: null }) }}
          />
          <div className="seg">
            {ONLY.map(k => (
              <button
                key={k} className={only === k ? 'on' : ''}
                onClick={() => update({ only: k === 'all' ? null : k, page: null })}
              >
                {LABEL[k]}{counts ? ` (${num(counts[k])})` : ''}
              </button>
            ))}
          </div>
          <select
            value={limit}
            onChange={e => update({
              rows: Number(e.target.value) === DEFAULT_LIMIT ? null : e.target.value, page: null,
            })}
          >
            {LIMITS.map(l => <option key={l} value={l}>{l} rows</option>)}
          </select>
        </div>
      </div>

      {error ? <ErrorBox message={error} /> : !rows ? <Loading rows={7} /> : rows.length === 0 ? (
        <Empty
          title={q ? `No runs match "${q}"` : only === 'all' ? 'No pipeline runs yet' : 'Nothing matches this filter'}
          text={q
            ? 'Search takes a run number, the start of a commit hash or part of a branch name.'
            : only === 'all' ? 'Runs appear here once your CI server starts sending events.' : ''}
        />
      ) : (
        <div className="table-wrap">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Run</th><th>Status</th><th>Commit</th><th>Branch</th>
                  <th className="num">Checks run</th><th className="num">Duration</th><th className="num">Queue</th>
                  <th>Plan</th><th>Started</th><th />
                </tr>
              </thead>
              <tbody>
                {rows.map(p => (
                  <tr key={p.id} className="clickable" onClick={() => open(p.id)}>
                    <td className="mono">#{p.id}</td>
                    <td><StatusBadge status={p.status} /></td>
                    <td className="mono">{shortSha(p.commit_sha)}</td>
                    <td className="mono">{p.ref.length > 26 ? p.ref.slice(0, 26) + '…' : p.ref}</td>
                    <td className="num">
                      {p.tests_run ? num(p.tests_run)
                        : p.linked_role === 'execution'
                          ? <span className="muted" title="This run only requested the plan">plan → #{p.linked_run_id}</span>
                          : '-'}
                    </td>
                    <td className="num">{dur(p.duration_s)}</td>
                    <td className="num">{p.queue_time_s ? dur(p.queue_time_s) : '-'}</td>
                    <td>{p.optimized || (p.linked_role === 'plan' && p.linked_optimized)
                      ? <span className="badge accent">Optimised</span>
                      : <span className="badge neutral">Full suite</span>}</td>
                    <td className="mono" title={p.started_at ?? ''}>{ago(p.started_at)}</td>
                    <td style={{ textAlign: 'right' }}><Icon.arrow className="muted" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="table-foot">
            <span>{num(first)}-{num(last)} of {num(total)}</span>
            <span className="pager">
              <button
                className="btn ghost sm" disabled={page <= 1}
                onClick={() => update({ page: page - 1 > 1 ? String(page - 1) : null })}
              >
                Newer
              </button>
              <span>Page {num(page)} of {num(pages)}</span>
              <button
                className="btn ghost sm" disabled={page >= pages}
                onClick={() => update({ page: String(page + 1) })}
              >
                Older
              </button>
            </span>
          </div>
        </div>
      )}
    </>
  )
}
