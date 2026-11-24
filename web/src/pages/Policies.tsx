import { useEffect, useState } from 'react'
import { api, auth, type Policy, type Project } from '../lib/api'
import { Empty, ErrorBox, Icon, Loading, ModeBadge, Notice, SectionHead } from '../components/ui'

const TYPE_LABEL: Record<string, string> = {
  skip_docs_only: 'Skip docs-only changes',
  test_selection: 'Test selection under a time budget',
  sharding: 'Shard distribution',
  fail_fast: 'Risk-first job ordering',
  flaky_quarantine: 'Flaky test quarantine',
}

const TYPE_HINT: Record<string, string> = {
  skip_docs_only: 'Skip the pipeline when a commit touches only documentation.',
  test_selection: 'Run the tests most likely to fail, within a share of the usual time.',
  sharding: 'Split the selected tests across parallel shards by predicted duration.',
  fail_fast: 'When risk is high, run fast checks before the slow ones.',
  flaky_quarantine: 'Keep known-flaky tests out of the selected set.',
}

/** Підписи параметрів: ключ у базі короткий, людині потрібне пояснення. */
const PARAM_LABEL: Record<string, string> = {
  budget_ratio: 'Time budget (share of full run)',
  budget_s: 'Time budget, seconds',
  min_keep_ratio: 'Minimum share of the suite to always run',
  protect_default_branch: 'Never reduce on the default branch',
  max_shards: 'Maximum shards',
  target_s: 'Target seconds per shard',
  risk_threshold: 'Risk threshold',
  keep_lint: 'Still run lint',
}

const MODES = [
  { key: 'off', label: 'Observe', help: 'Collect data only. No plan is computed.' },
  { key: 'dry_run', label: 'Dry run', help: 'The plan is computed and shown, but the pipeline still runs the full suite.' },
  { key: 'active', label: 'Active', help: 'The plan is applied to the pipeline.' },
]

type Draft = { enabled: boolean; branch_pattern: string; params: Record<string, unknown> }

function PolicyCard({ policy, projectId, onSaved }: {
  policy: Policy; projectId: number; onSaved: (p: Policy) => void
}) {
  const [draft, setDraft] = useState<Draft>({
    enabled: policy.enabled,
    branch_pattern: policy.branch_pattern,
    params: { ...(policy.params ?? {}) },
  })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const dirty =
    draft.enabled !== policy.enabled ||
    draft.branch_pattern !== policy.branch_pattern ||
    JSON.stringify(draft.params) !== JSON.stringify(policy.params ?? {})

  async function save() {
    setBusy(true); setErr('')
    try {
      onSaved(await api.updatePolicy(projectId, policy.id, draft))
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(false) }
  }

  function setParam(k: string, v: unknown) {
    setDraft(d => ({ ...d, params: { ...d.params, [k]: v } }))
  }

  return (
    <div className={`card${draft.enabled ? '' : ' muted-card'}`} style={{ marginBottom: 11 }}>
      <div className="row" style={{ alignItems: 'flex-start', gap: 14, marginBottom: 12 }}>
        <div style={{ flex: '1 1 300px', minWidth: 0 }}>
          <h3 style={{ fontSize: 14, color: 'var(--ink)', marginBottom: 4 }}>
            {TYPE_LABEL[policy.type] ?? policy.name}
          </h3>
          <p style={{ margin: 0, fontSize: 12.5, color: 'var(--muted)' }}>
            {TYPE_HINT[policy.type] ?? ''}
          </p>
        </div>
        <label className="switch" title={draft.enabled ? 'Enabled' : 'Disabled'}>
          <input
            type="checkbox" checked={draft.enabled} disabled={!auth.isDevops}
            onChange={e => setDraft(d => ({ ...d, enabled: e.target.checked }))}
          />
          <span className="track"><span className="knob" /></span>
          <span className="switch-label">{draft.enabled ? 'Enabled' : 'Disabled'}</span>
        </label>
      </div>

      {err && <ErrorBox message={err} />}

      <div className="param-grid">
        <div className="field" style={{ margin: 0 }}>
          <label>Branches</label>
          <input
            value={draft.branch_pattern} disabled={!auth.isDevops}
            onChange={e => setDraft(d => ({ ...d, branch_pattern: e.target.value }))}
            placeholder="*"
          />
        </div>

        {Object.entries(draft.params).map(([k, v]) => (
          <div className="field" style={{ margin: 0 }} key={k}>
            <label>{PARAM_LABEL[k] ?? k}</label>
            {typeof v === 'boolean' ? (
              <label className="switch sm">
                <input
                  type="checkbox" checked={v} disabled={!auth.isDevops}
                  onChange={e => setParam(k, e.target.checked)}
                />
                <span className="track"><span className="knob" /></span>
                <span className="switch-label">{v ? 'on' : 'off'}</span>
              </label>
            ) : (
              <input
                type="number" value={String(v)} disabled={!auth.isDevops}
                step={typeof v === 'number' && !Number.isInteger(v) ? 0.01 : 1}
                onChange={e => setParam(k, e.target.value === '' ? 0 : Number(e.target.value))}
              />
            )}
          </div>
        ))}
      </div>

      {auth.isDevops && dirty && (
        <div className="row" style={{ marginTop: 13 }}>
          <button className="btn sm" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : <><Icon.check />Save changes</>}
          </button>
          <button
            className="btn ghost sm" disabled={busy}
            onClick={() => setDraft({
              enabled: policy.enabled,
              branch_pattern: policy.branch_pattern,
              params: { ...(policy.params ?? {}) },
            })}
          >
            Reset
          </button>
        </div>
      )}
    </div>
  )
}

export default function Policies({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<Policy[] | null>(null)
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setRows(null); setError('')
    api.policies(projectId).then(setRows).catch(e => setError(e.message))
    api.project(projectId).then(setProject).catch(() => { /* not critical */ })
  }, [projectId])

  async function changeMode(mode: string) {
    setBusy(true)
    try {
      setProject(await api.setMode(projectId, mode))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(false) }
  }

  if (error) return <ErrorBox message={error} />

  const currentMode = MODES.find(m => m.key === project?.plan_mode)

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Policies</h2>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-head">
          <h3><Icon.shield />Rollout mode</h3>
          {project && <ModeBadge mode={project.plan_mode} />}
        </div>

        {auth.isDevops ? (
          <div className="seg" style={{ marginBottom: 12 }}>
            {MODES.map(m => (
              <button
                key={m.key} className={project?.plan_mode === m.key ? 'on' : ''}
                disabled={busy} onClick={() => changeMode(m.key)}
              >
                {m.label}
              </button>
            ))}
          </div>
        ) : (
          <Notice plain>Only a DevOps engineer can change the rollout mode.</Notice>
        )}

        {currentMode && (
          <p style={{ fontSize: 12.5, color: 'var(--ink-2)', margin: '0 0 8px' }}>{currentMode.help}</p>
        )}
        <p style={{ fontSize: 12.5, color: 'var(--muted)', margin: 0 }}>
          Recommended order: observe, dry run, active.
        </p>
      </div>

      <SectionHead
        title="Optimisation policies"
        right={rows ? <span className="badge neutral sm">{rows.length}</span> : undefined}
      />

      {!rows ? <Loading rows={5} /> : rows.length === 0 ? (
        <Empty title="No policies configured"  />
      ) : rows.map(p => (
        <PolicyCard
          key={p.id} policy={p} projectId={projectId}
          onSaved={saved => setRows(rs => rs?.map(x => (x.id === saved.id ? saved : x)) ?? rs)}
        />
      ))}
    </>
  )
}
