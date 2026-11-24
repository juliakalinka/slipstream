import { useEffect, useState } from 'react'
import { api, auth, type Project, type WebhookInfo } from '../lib/api'
import { BRAND } from '../brand'
import { ErrorBox, Icon, Loading, Notice, SectionHead } from '../components/ui'

type Tab = 'gitlab' | 'github'

function CopyBlock({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <div style={{ position: 'relative' }}>
      {label && <div className="side-label" style={{ padding: '0 0 7px' }}>{label}</div>}
      <button
        className="btn ghost sm"
        style={{ position: 'absolute', right: 8, top: label ? 26 : 8, zIndex: 2 }}
        onClick={() => {
          navigator.clipboard?.writeText(text)
          setDone(true); setTimeout(() => setDone(false), 1600)
        }}
      >
        {done ? <><Icon.check />Copied</> : 'Copy'}
      </button>
      <pre className="code">{text}</pre>
    </div>
  )
}

const GITLAB_SNIPPET = (api_: string, pid: number) => `# .gitlab-ci.yml: add these three jobs.
# 1. ask for a plan  2. run exactly that plan  3. report results back

stages: [plan, run, report]

variables:
  CICD_OPT_API: "${api_}"
  CICD_OPT_PROJECT: "${pid}"

optimize:
  stage: plan
  image: python:3.12-slim
  variables: { GIT_DEPTH: "0" }
  before_script: [pip install --quiet httpx]
  script:
    - python cli/cicd_opt.py plan
        --ref "$CI_COMMIT_REF_NAME" --sha "$CI_COMMIT_SHA"
        --base "origin/$CI_DEFAULT_BRANCH"
        --output pipeline.yml --timeout 10
  artifacts: { paths: [pipeline.yml], expire_in: 1 week }

run-pipeline:
  stage: run
  trigger:
    include: [{ artifact: pipeline.yml, job: optimize }]
    strategy: depend

report:
  stage: report
  image: curlimages/curl:latest
  needs: [{ job: run-pipeline, artifacts: true }]
  when: always          # failures are the most valuable signal
  script:
    - |
      for f in $(find . -name 'junit*.xml' -o -name 'TEST-*.xml'); do
        curl -sS --retry 3 -X POST \\
          -H "X-CICD-Token: $CICD_OPT_SECRET" \\
          --data-binary "@$f" \\
          "$CICD_OPT_API/webhooks/junit/$CICD_OPT_PROJECT?external_id=$CI_PIPELINE_ID&commit_sha=$CI_COMMIT_SHA&ref=$CI_COMMIT_REF_NAME"
      done
  allow_failure: true`

const GITHUB_SNIPPET = (api_: string, pid: number) => `# .github/workflows/tests.yml: reporting step.
#
# GitHub Actions has no dynamic child pipelines, so ${BRAND.name} can observe
# this workflow but cannot rewrite it. Per-test results never appear in the
# GitHub API, so the workflow has to send the JUnit report itself.

      - name: Report test results
        if: always()
        env:
          CICD_OPT_API: "${api_}"
          CICD_OPT_SECRET: \${{ secrets.CICD_OPT_SECRET }}
        run: |
          for f in $(find . -name 'junit*.xml' -o -name 'TEST-*.xml'); do
            curl -sS --retry 3 -X POST \\
              -H "X-CICD-Token: $CICD_OPT_SECRET" \\
              --data-binary "@$f" \\
              "$CICD_OPT_API/webhooks/junit/${pid}?external_id=$GITHUB_RUN_ID&commit_sha=$GITHUB_SHA&ref=$GITHUB_REF_NAME"
          done

# Pipeline-level events can also be forwarded. Add a repository webhook for
# "workflow_run" pointing at:
#   ${api_}/webhooks/github/${pid}
# and set its secret to the same value; the signature is verified as HMAC-SHA256.`

export default function Connect({ projectId }: { projectId: number }) {
  const [info, setInfo] = useState<WebhookInfo | null>(null)
  const [project, setProject] = useState<Project | null>(null)
  const [secret, setSecret] = useState('')
  const [tab, setTab] = useState<Tab>('gitlab')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setInfo(null); setSecret(''); setError('')
    api.webhookInfo(projectId).then(i => {
      setInfo(i)
      setTab(i.provider === 'github' ? 'github' : 'gitlab')
    }).catch(e => setError(e.message))
    api.project(projectId).then(setProject).catch(() => { /* not critical */ })
  }, [projectId])

  async function rotate() {
    setBusy(true); setError('')
    try {
      const r = await api.rotateWebhookSecret(projectId)
      setSecret(r.secret)
      setInfo(i => (i ? { ...i, configured: true } : i))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(false) }
  }

  if (error) return <ErrorBox message={error} />

  const origin = `${window.location.origin}/api/v1`
  const snippet = tab === 'gitlab'
    ? GITLAB_SNIPPET(origin, projectId)
    : GITHUB_SNIPPET(origin, projectId)

  return (
    <>
      <div className="page-head">
        <div>
          <h2>Connect CI</h2>
        </div>
      </div>

      {!info ? <Loading rows={5} /> : (
        <>
          <div className="grid k2" style={{ marginBottom: 14 }}>
            <div className="card">
              <div className="card-head">
                <h3>Observe (any CI)</h3>
              </div>
              <p style={{ margin: 0, fontSize: 13, color: 'var(--ink-2)' }}>
                Your CI posts pipeline events and JUnit reports. The pipeline itself does not change.
              </p>
            </div>
            <div className="card">
              <div className="card-head">
                <h3>Control (GitLab CI)</h3>
              </div>
              <p style={{ margin: 0, fontSize: 13, color: 'var(--ink-2)' }}>
                One extra job asks for a plan and runs it as a child pipeline.
                Not available for GitHub Actions.
              </p>
            </div>
          </div>

          <SectionHead
            title="Webhook secret"
            right={info.configured
              ? <span className="badge ok">configured</span>
              : <span className="badge warn">not set</span>}
          />

          <div className="card" style={{ marginBottom: 14 }}>
            {!info.configured && (
              <Notice>
                Webhooks are rejected until a secret is set.
              </Notice>
            )}

            {secret ? (
              <>
                <CopyBlock text={secret} label="Secret (shown once)" />
                <p style={{ fontSize: 12.5, color: 'var(--warn)', margin: '10px 0 0' }}>
                  Copy it now. It is not stored in a readable form and will not be shown again.
                  Put it in your CI as a protected variable named <b>CICD_OPT_SECRET</b>.
                </p>
              </>
            ) : (
              <div className="row">
                {auth.isDevops ? (
                  <button className="btn" onClick={rotate} disabled={busy}>
                    {busy ? 'Generating…'
                      : info.configured ? <><Icon.refresh />Rotate secret</> : <>Generate secret</>}
                  </button>
                ) : (
                  <span className="muted" style={{ fontSize: 12.5 }}>
                    Only a DevOps engineer can manage the webhook secret.
                  </span>
                )}
                {info.configured && (
                  <span className="muted" style={{ fontSize: 12.5 }}>
                    Rotating invalidates the previous secret immediately.
                  </span>
                )}
              </div>
            )}
          </div>

          <SectionHead title="Endpoints" />
          <div className="table-wrap" style={{ marginBottom: 14 }}>
            <div className="table-scroll">
              <table>
                <thead><tr><th>Purpose</th><th>Endpoint</th><th>Auth header</th></tr></thead>
                <tbody>
                  <tr>
                    <td className="strong">GitLab pipeline hook</td>
                    <td className="mono">{origin}/webhooks/gitlab/{projectId}</td>
                    <td className="mono">{info.headers.gitlab}</td>
                  </tr>
                  <tr>
                    <td className="strong">GitHub workflow_run</td>
                    <td className="mono">{origin}/webhooks/github/{projectId}</td>
                    <td className="mono">{info.headers.github}</td>
                  </tr>
                  <tr>
                    <td className="strong">JUnit report</td>
                    <td className="mono">{origin}/webhooks/junit/{projectId}</td>
                    <td className="mono">{info.headers.junit}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div className="table-foot">
              <span>Repeated delivery is safe: an event updates the existing record.</span>
              <span>{project?.ci_provider ?? info.provider}</span>
            </div>
          </div>

          <SectionHead
            title="Pipeline configuration"
            right={
              <div className="seg">
                <button className={tab === 'gitlab' ? 'on' : ''} onClick={() => setTab('gitlab')}>GitLab CI</button>
                <button className={tab === 'github' ? 'on' : ''} onClick={() => setTab('github')}>GitHub Actions</button>
              </div>
            }
          />
          <div className="card">
            <CopyBlock text={snippet} />
          </div>
        </>
      )}
    </>
  )
}
