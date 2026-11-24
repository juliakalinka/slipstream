/** REST API client. The token lives in localStorage. */

const TOKEN_KEY = 'cicd_token'
const ROLE_KEY = 'cicd_role'
const EMAIL_KEY = 'cicd_email'

/**
 * Підписники на зміну стану входу.
 *
 * Маркер лежить у сховищі браузера, а воно не є станом React: записати
 * маркер і сподіватися, що застосунок це помітить, не можна. Саме через це
 * вхід колись виглядав як несправність — маркер зберігався, але сторінка
 * входу лишалася на місці, бо нічого не перемальовувалося.
 */
const authListeners = new Set<() => void>()
const notifyAuth = () => authListeners.forEach(fn => fn())

export const auth = {
  /** Підписатися на вхід і вихід. Повертає функцію скасування підписки. */
  subscribe(fn: () => void): () => void {
    authListeners.add(fn)
    return () => { authListeners.delete(fn) }
  },
  get token() {
    try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
  },
  get role() {
    try { return localStorage.getItem(ROLE_KEY) ?? '' } catch { return '' }
  },
  get email() {
    try { return localStorage.getItem(EMAIL_KEY) ?? '' } catch { return '' }
  },
  save(token: string, role: string, email: string) {
    try {
      localStorage.setItem(TOKEN_KEY, token)
      localStorage.setItem(ROLE_KEY, role)
      localStorage.setItem(EMAIL_KEY, email)
    } catch { /* private browsing */ }
    notifyAuth()
  },
  clear() {
    try {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(ROLE_KEY)
      localStorage.removeItem(EMAIL_KEY)
    } catch { /* nothing to do */ }
    notifyAuth()
  },
  get isDevops() { return this.role === 'devops' },
  /** Зведення за період розсилає керівник або DevOps-інженер. */
  get canSendDigest() { return this.role === 'devops' || this.role === 'lead' },
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

/** Options that change how a failed response is handled. */
type Opts = {
  /**
   * Treat 401 as an expired session: drop the token and go to the sign-in
   * page. Switched off for the sign-in request itself, where 401 simply
   * means the password is wrong.
   */
  sessionGuard?: boolean
  /** Called with the headers of a successful response. */
  onHeaders?: (headers: Headers) => void
}

async function request<T>(path: string, init: RequestInit = {}, opts: Opts = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  }
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`

  const res = await fetch(`/api/v1${path}`, { ...init, headers })

  if (res.status === 401 && opts.sessionGuard !== false) {
    auth.clear()
    window.location.hash = '#/login'
    throw new ApiError(401, 'Your session expired, please sign in again')
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch { /* body is not JSON */ }
    // Too many failed sign-in attempts: say when it is worth retrying.
    const retry = Number(res.headers.get('Retry-After') ?? 0)
    if (res.status === 429 && retry > 0) {
      detail += ` Wait about ${Math.ceil(retry / 60)} min.`
    }
    throw new ApiError(res.status, detail)
  }
  opts.onHeaders?.(res.headers)
  if (res.status === 204) return undefined as T
  const text = await res.text()
  try { return JSON.parse(text) as T } catch { return text as unknown as T }
}

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; role: string; email: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }, { sessionGuard: false }),

  projects: () => request<Project[]>('/projects'),
  project: (id: number) => request<Project>(`/projects/${id}`),
  setMode: (id: number, plan_mode: string) =>
    request<Project>(`/projects/${id}/mode`, {
      method: 'PUT', body: JSON.stringify({ plan_mode }),
    }),

  dashboard: (id: number, days = 30) => request<Dashboard>(`/projects/${id}/dashboard?days=${days}`),
  /** One page of runs, newest first, and how many runs match in total. */
  pipelines: async (id: number, f: PipelineQuery = {}) => {
    const qs = new URLSearchParams({ limit: String(f.limit ?? 50), offset: String(f.offset ?? 0) })
    if (f.status) qs.set('status', f.status)
    if (f.optimized !== undefined) qs.set('optimized', String(f.optimized))
    if (f.q) qs.set('q', f.q)
    let total = 0
    const rows = await request<Pipeline[]>(`/projects/${id}/pipelines?${qs}`, {}, {
      onHeaders: h => { total = Number(h.get('X-Total-Count') ?? 0) },
    })
    return { rows, total }
  },
  pipeline: (id: number) => request<PipelineDetail>(`/pipelines/${id}`),
  flaky: (id: number) => request<FlakyTest[]>(`/projects/${id}/flaky?limit=30`),
  alerts: (id: number, limit = 50) => request<Alert[]>(`/projects/${id}/alerts?limit=${limit}`),
  ackAlert: (projectId: number, alertId: number) =>
    request<Alert>(`/projects/${projectId}/alerts/${alertId}/ack`, { method: 'PUT' }),
  policies: (id: number) => request<Policy[]>(`/projects/${id}/policies`),
  models: (id: number) => request<ModelVersion[]>(`/projects/${id}/models`),
  retrain: (id: number) => request<{ accepted: boolean }>(`/projects/${id}/models/retrain`, { method: 'POST' }),

  recommendations: (id: number) => request<Recommendation[]>(`/projects/${id}/recommendations`),
  generateRecos: (id: number) =>
    request<Recommendation[]>(`/projects/${id}/recommendations/generate`, { method: 'POST' }),
  applyReco: (id: number) => request<ApplyResult>(`/recommendations/${id}/apply`, { method: 'PUT' }),
  rejectReco: (id: number) => request<ApplyResult>(`/recommendations/${id}/reject`, { method: 'PUT' }),

  previewPlan: (projectId: number, sha: string) =>
    request<{ plan: Plan; gitlab_yaml: string }>(`/pipelines/plan/preview/${projectId}/${sha}`),

  createProject: (body: NewProject) =>
    request<Project>('/projects', { method: 'POST', body: JSON.stringify(body) }),

  updatePolicy: (projectId: number, policyId: number, body: PolicyPatch) =>
    request<Policy>(`/projects/${projectId}/policies/${policyId}`, {
      method: 'PUT', body: JSON.stringify(body),
    }),

  activateModel: (modelId: number) =>
    request<ModelVersion>(`/models/${modelId}/activate`, { method: 'PUT' }),

  report: (projectId: number, from: string, to: string) =>
    request<PeriodReport>(`/projects/${projectId}/report?date_from=${from}&date_to=${to}`),
  /** The CSV needs the bearer token, so it cannot be a plain link. */
  reportCsv: async (projectId: number, from: string, to: string): Promise<Blob> => {
    const res = await fetch(
      `/api/v1/projects/${projectId}/report?date_from=${from}&date_to=${to}&format=csv`,
      { headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {} },
    )
    if (!res.ok) throw new ApiError(res.status, `Could not build the report (${res.status})`)
    return res.blob()
  },
  sendDigest: (projectId: number, from: string, to: string) =>
    request<{ channels: Record<string, boolean> }>(
      `/projects/${projectId}/report/send?date_from=${from}&date_to=${to}`, { method: 'POST' }),

  problems: (projectId: number) => request<ProblemReport[]>(`/projects/${projectId}/problems`),
  reportProblem: (projectId: number, body: NewProblem) =>
    request<ProblemReport>(`/projects/${projectId}/problems`, {
      method: 'POST', body: JSON.stringify(body),
    }),
  resolveProblem: (projectId: number, reportId: number) =>
    request<ProblemReport>(`/projects/${projectId}/problems/${reportId}/resolve`, { method: 'PUT' }),

  webhookInfo: (projectId: number) => request<WebhookInfo>(`/projects/${projectId}/webhook`),
  rotateWebhookSecret: (projectId: number) =>
    request<WebhookSecret>(`/projects/${projectId}/webhook-secret`, { method: 'POST' }),
}

// ------------------------------------------------------------------ types
export interface Project {
  id: number; name: string; repo_url: string; ci_provider: string
  default_branch: string; plan_mode: string; created_at: string
}
export interface Dashboard {
  project: { id: number; name: string; mode: string }
  window_days: number
  /** When the project last ran, and the shortest window that reaches it. */
  last_run_at: string | null
  suggested_days: number | null
  pipelines: {
    total: number; success: number; failed: number; success_rate: number
    mean_duration_s: number; median_duration_s: number; p95_duration_s: number
    mean_queue_s: number; optimized_share: number
    planned_share: number; potential_saving: number
  }
  dora: {
    deployment_frequency_per_day: number; lead_time_hours: number
    change_failure_rate: number; time_to_restore_hours: number; deployments: number
  }
  slowest_jobs: { name: string; mean_duration_s: number; runs: number; max_duration_s: number }[]
  flakiest_tests: FlakyTest[]
  trend: { date: string; count: number; mean_duration_s: number; success_rate: number }[]
}
export interface PipelineQuery {
  status?: string; optimized?: boolean; q?: string; limit?: number; offset?: number
}
export interface Pipeline {
  id: number; project_id: number; commit_sha: string; ref: string; status: string
  duration_s: number; queue_time_s: number; optimized: boolean
  started_at: string | null; finished_at: string | null
  /** checks actually executed in this run, and how many of them failed */
  tests_run: number; tests_failed: number
  /** the other half of a GitLab parent/child pair: where the plan was issued or executed */
  linked_run_id: number | null; linked_role: 'plan' | 'execution' | null; linked_optimized: boolean
}
export interface SuiteChecks { suite: string; ran: number; not_run: number; failed: number }
export interface PredictionRow {
  type: string; label: string; unit: string
  predicted: number; actual: number | null; error: number | null
  confidence: number; model_version: string | null
}
export interface PipelineDetail extends Pipeline {
  jobs: { id: number; name: string; stage: string; status: string; duration_s: number; shard_index: number | null }[]
  predictions: PredictionRow[]
  plan_snapshot: Record<string, unknown>
  commit_author: string; commit_message: string; files_changed: number
  changed_paths: string[]
  plan_from_run: number | null
  tests_total: number; checks_by_suite: SuiteChecks[]; failed_checks: string[]
}
export interface FlakyTest {
  id: number; suite: string; name: string; flaky_score: number
  fail_rate: number; runs: number; quarantined: boolean
  order_dependent?: boolean; flaky_category?: string
}
export interface Alert {
  id: number; kind: string; severity: string; message: string
  details: Record<string, unknown>; acknowledged: boolean; created_at: string
}
export interface Policy {
  id: number; name: string; type: string; params: Record<string, unknown>
  enabled: boolean; branch_pattern: string
}
export interface ModelVersion {
  id: number; type: string; version: string; algorithm: string
  metrics: Record<string, number | string | Record<string, number>>
  train_samples: number; trained_at: string; is_active: boolean
}
export interface Recommendation {
  id: number; type: string; title: string; description: string
  evidence: Record<string, unknown>; expected_gain_s: number
  status: string; created_at: string
}
export interface ApplyResult { id: number; status: string; applied?: string[]; warnings?: string[] }
export interface Plan {
  mode: string; risk: number; risk_threshold: number
  selected_tests: number; total_tests: number; shards: number
  est_duration_s: number; baseline_duration_s: number; saved_s: number; saving_ratio: number
  rationale: string[]; fallbacks: string[]
  model_versions: Record<string, string>
  jobs: { name: string; stage: string; tests: number; est_duration_s: number; shard: string | null }[]
}

export interface NewProject {
  name: string; repo_url?: string; ci_provider?: string
  external_id?: string; default_branch?: string; access_token?: string
}
export interface PolicyPatch {
  name?: string; params?: Record<string, unknown>
  enabled?: boolean; branch_pattern?: string
}
export interface WebhookInfo {
  configured: boolean
  provider: string
  endpoints: { gitlab: string; github: string; junit: string }
  headers: { gitlab: string; github: string; junit: string }
}
export interface WebhookSecret {
  secret: string; note: string
  endpoints: { gitlab: string; github: string; junit: string }
}
export interface PeriodReport {
  project: { id: number; name: string; mode: string }
  period: { from: string; to: string; days: number }
  pipelines: {
    total: number; success: number; failed: number; success_rate: number
    mean_duration_s: number; median_duration_s: number; p95_duration_s: number
    mean_queue_s: number
  }
  optimisation: {
    optimized_runs: number; optimized_share: number; saved_hours: number; saved_seconds: number
    planned_share: number; potential_saving: number
  }
  dora: Dashboard['dora']
  quality: {
    alerts_raised: number; alerts_open: number
    recommendations_applied: number; recommendations_rejected: number
    tests_quarantined: number
  }
  slowest_jobs: Dashboard['slowest_jobs']
  daily: {
    date: string; count: number; success: number; failed: number
    success_rate: number; mean_duration_s: number; optimized: number
  }[]
  generated_at: string
}
export interface NewProblem {
  subject_type: 'alert' | 'pipeline' | 'recommendation' | 'test_case' | 'other'
  subject_id?: number
  note: string
}
export interface ProblemReport {
  id: number; project_id: number; subject_type: string; subject_id: number | null
  note: string; resolved: boolean; created_at: string; resolved_at: string | null
  reported_by: string
}
