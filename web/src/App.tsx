import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import {
  NavLink, Navigate, Route, Routes, useLocation, useNavigate,
} from 'react-router-dom'
import { BRAND } from './brand'
import { api, auth, type NewProject, type Project } from './lib/api'
import { BrandMark, ErrorBox, Icon, ModeBadge } from './components/ui'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Pipelines from './pages/Pipelines'
import PipelineView from './pages/PipelineView'
import Recommendations from './pages/Recommendations'
import Alerts from './pages/Alerts'
import FlakyTests from './pages/FlakyTests'
import Policies from './pages/Policies'
import Models from './pages/Models'
import Connect from './pages/Connect'
import Reports from './pages/Reports'

const NAV = [
  { to: '/dashboard', label: 'Overview', icon: Icon.dashboard, counter: false },
  { to: '/pipelines', label: 'Pipelines', icon: Icon.pipelines, counter: false },
  { to: '/alerts', label: 'Alerts', icon: Icon.alert, counter: true },
  { to: '/recommendations', label: 'Recommendations', icon: Icon.bulb, counter: false },
  { to: '/flaky', label: 'Flaky tests', icon: Icon.flaky, counter: false },
  { to: '/reports', label: 'Reports', icon: Icon.report, counter: false },
]

const NAV_SETUP = [
  { to: '/policies', label: 'Policies', icon: Icon.policies, counter: false },
  { to: '/models', label: 'Models', icon: Icon.models, counter: false },
  { to: '/connect', label: 'Connect CI', icon: Icon.plug, counter: false },
]

const ROLE_LABEL: Record<string, string> = {
  devops: 'DevOps engineer',
  developer: 'Developer',
  lead: 'Engineering lead',
}

const PROJECT_KEY = 'cicd_project'
const PROVIDERS = ['gitlab', 'github', 'jenkins', 'synthetic']

function initials(email: string): string {
  const name = email.split('@')[0] ?? '?'
  const parts = name.split(/[._-]/).filter(Boolean)
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)).toUpperCase()
}

/** Підключення проєкту прямо з інтерфейсу: інакше єдиний шлях - через API. */
function NewProjectModal({ onClose, onCreated }: {
  onClose: () => void; onCreated: (p: Project) => void
}) {
  const [form, setForm] = useState<NewProject>({
    name: '', repo_url: '', ci_provider: 'gitlab', default_branch: 'main',
  })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', key)
    return () => document.removeEventListener('keydown', key)
  }, [onClose])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      onCreated(await api.createProject({ ...form, external_id: form.name }))
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Request failed')
    } finally { setBusy(false) }
  }

  return (
    <div className="modal-back" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <form className="modal" onSubmit={submit}>
        <h3>Connect a project</h3>
        <p className="sub">
          A new project starts in dry-run mode: plans are computed but not applied.
        </p>

        {err && <ErrorBox message={err} />}

        <div className="field">
          <label>Name</label>
          <input
            autoFocus required value={form.name} placeholder="team/service"
            onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
          />
        </div>
        <div className="field">
          <label>Repository URL</label>
          <input
            value={form.repo_url} placeholder="https://gitlab.com/team/service"
            onChange={e => setForm(f => ({ ...f, repo_url: e.target.value }))}
          />
        </div>
        <div className="param-grid">
          <div className="field" style={{ margin: 0 }}>
            <label>CI provider</label>
            <select
              value={form.ci_provider} style={{ width: '100%' }}
              onChange={e => setForm(f => ({ ...f, ci_provider: e.target.value }))}
            >
              {PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>Default branch</label>
            <input
              value={form.default_branch}
              onChange={e => setForm(f => ({ ...f, default_branch: e.target.value }))}
            />
          </div>
        </div>

        <div className="modal-foot">
          <button type="button" className="btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="btn" disabled={busy || !form.name.trim()}>
            {busy ? 'Connecting…' : 'Connect'}
          </button>
        </div>
      </form>
    </div>
  )
}

function ProjectPicker({ projects, current, onPick, onAdd }: {
  projects: Project[]; current: Project | undefined
  onPick: (id: number) => void; onAdd: () => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const click = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', click)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('mousedown', click)
      document.removeEventListener('keydown', key)
    }
  }, [open])

  return (
    <div className="picker" ref={box}>
      <button
        className="picker-btn" onClick={() => setOpen(o => !o)}
        aria-haspopup="listbox" aria-expanded={open}
      >
        <span className="nm">{current?.name ?? 'No project'}</span>
        <Icon.chevron />
      </button>

      {current && (
        <div className="picker-meta">
          <span>{current.ci_provider}</span>
          <ModeBadge mode={current.plan_mode} />
        </div>
      )}

      {open && (
        <div className="picker-list" role="listbox">
          {projects.length === 0 && (
            <div style={{ padding: '10px 9px', fontSize: 12.5, color: 'var(--muted)' }}>
              No projects connected
            </div>
          )}
          {projects.map(p => (
            <button
              key={p.id} role="option" aria-selected={p.id === current?.id}
              className={p.id === current?.id ? 'on' : ''}
              onClick={() => { onPick(p.id); setOpen(false) }}
            >
              <span className="nm">{p.name}</span>
              {p.id === current?.id && <Icon.check />}
            </button>
          ))}
          {auth.isDevops && (
            <button
              style={{
                borderTop: '1px solid var(--line)', marginTop: 4,
                paddingTop: 9, color: 'var(--acc)',
              }}
              onClick={() => { setOpen(false); onAdd() }}
            >
              <Icon.plus /><span className="nm">Connect a project</span>
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function Shell({ projectId, setProjectId }: {
  projectId: number; setProjectId: (id: number) => void
}) {
  const nav = useNavigate()
  const loc = useLocation()
  const [projects, setProjects] = useState<Project[]>([])
  const [openAlerts, setOpenAlerts] = useState(0)
  const [adding, setAdding] = useState(false)

  const loadProjects = useCallback(() => {
    api.projects()
      .then(ps => {
        setProjects(ps)
        if (!projectId && ps.length) setProjectId(ps[0].id)
      })
      .catch(() => { /* surfaced on the pages themselves */ })
  }, [])

  useEffect(() => { loadProjects() }, [loadProjects])

  // Лічильник біля пункту меню: непідтверджені аномалії мають бути помітні
  // з будь-якої сторінки, інакше про них дізнаються надто пізно.
  useEffect(() => {
    if (!projectId) return
    api.alerts(projectId, 100)
      .then(as => setOpenAlerts(as.filter(a => !a.acknowledged).length))
      .catch(() => setOpenAlerts(0))
  }, [projectId, loc.pathname])

  const current = projects.find(p => p.id === projectId)
  const here = [...NAV, ...NAV_SETUP].find(n => loc.pathname.startsWith(n.to))
  const isDetail = /^\/pipelines\/\d+/.test(loc.pathname)

  function navLink(n: { to: string; label: string; icon: (p: { size?: number }) => React.ReactElement; counter: boolean }) {
    const I = n.icon
    return (
      <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? 'active' : '')}>
        <I />{n.label}
        {n.counter && openAlerts > 0 && <span className="nav-count alarm">{openAlerts}</span>}
      </NavLink>
    )
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <BrandMark />
          <div className="brand-text">
            <h1>{BRAND.name}</h1>
          </div>
        </div>

        <div className="side-section">
          <div className="side-label">Project</div>
          <ProjectPicker
            projects={projects} current={current}
            onPick={id => { setProjectId(id); localStorage.setItem(PROJECT_KEY, String(id)) }}
            onAdd={() => setAdding(true)}
          />
        </div>

        <nav className="nav">
          {NAV.map(navLink)}
          <div className="side-label" style={{ padding: '15px 4px 8px' }}>Configuration</div>
          {NAV_SETUP.map(navLink)}
        </nav>

        <div className="sidebar-foot">
          <div className="avatar">{initials(auth.email)}</div>
          <div className="who">
            <b>{auth.email.split('@')[0]}</b>
            <span>{ROLE_LABEL[auth.role] ?? auth.role}</span>
          </div>
          <button
            className="icon-btn" title="Sign out" aria-label="Sign out"
            onClick={() => { auth.clear(); nav('/login') }}
          >
            <Icon.logout />
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="crumbs">
            <span className="muted">{current?.name ?? BRAND.name}</span>
            <span className="sep">/</span>
            {isDetail ? (
              <>
                <NavLink to="/pipelines">Pipelines</NavLink>
                <span className="sep">/</span>
                <span className="here">Run detail</span>
              </>
            ) : (
              <span className="here">{here?.label ?? 'Overview'}</span>
            )}
          </div>
          <div className="topbar-right">
            <button
              className="btn ghost sm" onClick={() => window.location.reload()}
              title="Reload data"
            >
              <Icon.refresh />Refresh
            </button>
          </div>
        </header>

        <main className="content">
          {projectId === 0 ? (
            // Доки перелік проєктів не завантажено, сторінки не можна малювати:
            // вони звернулися б до API з нульовим ідентифікатором.
            <div className="grid k4">
              {[0, 1, 2, 3].map(i => (
                <div key={i} className="card tile">
                  <div className="skeleton" style={{ height: 10, width: '52%' }} />
                  <div className="skeleton" style={{ height: 25, width: '68%', marginTop: 10 }} />
                </div>
              ))}
            </div>
          ) : (
            <Routes>
              <Route path="/dashboard" element={<Dashboard projectId={projectId} />} />
              <Route path="/pipelines" element={<Pipelines projectId={projectId} />} />
              <Route path="/pipelines/:id" element={<PipelineView />} />
              <Route path="/alerts" element={<Alerts projectId={projectId} />} />
              <Route path="/recommendations" element={<Recommendations projectId={projectId} />} />
              <Route path="/flaky" element={<FlakyTests projectId={projectId} />} />
              <Route path="/reports" element={<Reports projectId={projectId} />} />
              <Route path="/policies" element={<Policies projectId={projectId} />} />
              <Route path="/models" element={<Models projectId={projectId} />} />
              <Route path="/connect" element={<Connect projectId={projectId} />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          )}
        </main>
      </div>

      {adding && (
        <NewProjectModal
          onClose={() => setAdding(false)}
          onCreated={p => {
            setAdding(false)
            setProjects(ps => [...ps, p])
            setProjectId(p.id)
            localStorage.setItem(PROJECT_KEY, String(p.id))
            nav('/connect')
          }}
        />
      )}
    </div>
  )
}

export default function App() {
  // Стан входу береться із зовнішнього сховища через підписку. Просте
  // читання під час промальовування не працювало: зміна адреси перемальовує
  // лише <Routes>, а сам App - ні, тому після входу він ще вважав, що
  // користувач не ввійшов, і маршрут «*» одразу повертав на сторінку входу.
  const authed = useSyncExternalStore(auth.subscribe, () => Boolean(auth.token))
  const [projectId, setProjectId] = useState<number>(
    () => Number(localStorage.getItem(PROJECT_KEY) ?? 0) || 0,
  )

  if (!authed) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return <Shell projectId={projectId} setProjectId={setProjectId} />
}
