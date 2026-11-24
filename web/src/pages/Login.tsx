import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BRAND } from '../brand'
import { api, auth } from '../lib/api'
import { BrandMark, Icon } from '../components/ui'

export default function Login() {
  const nav = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: React.FormEvent, creds?: { email: string; password: string }) {
    e.preventDefault()
    const use = creds ?? { email, password }
    setBusy(true); setError('')
    try {
      const r = await api.login(use.email, use.password)
      auth.save(r.access_token, r.role, r.email)
      nav('/dashboard')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login">
        <div className="login-brand">
          <BrandMark size={34} />
          <div>
            <h1>{BRAND.name}</h1>
          </div>
        </div>

        <div className="login-card">
          <h2>Sign in</h2>

          {error && (
            <div className="error" role="alert">
              <Icon.alert /><span>{error}</span>
            </div>
          )}

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email" type="email" value={email} autoComplete="username"
                onChange={e => setEmail(e.target.value)} required
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password" type="password" value={password} autoComplete="current-password"
                onChange={e => setPassword(e.target.value)} required
              />
            </div>
            <button className="btn block" disabled={busy} type="submit">
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>

        </div>
      </div>
    </div>
  )
}
