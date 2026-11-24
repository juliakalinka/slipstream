import { useState } from 'react'
import { api, type NewProblem } from '../lib/api'
import { Icon } from './ui'

/**
 * «Повідомити про проблему»: кнопка, що розгортається в поле для пояснення.
 *
 * Хибне спрацювання помічає людина, яка знає проєкт, і найчастіше це не
 * DevOps-інженер. Тому елемент доступний усім ролям і стоїть поруч із тим,
 * про що повідомляють: біля сповіщення, біля плану прогону.
 */
export default function ReportProblem({ projectId, subject, subjectId, label = 'Report a problem', onSent }: {
  projectId: number
  subject: NewProblem['subject_type']
  subjectId?: number
  label?: string
  onSent?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [err, setErr] = useState('')

  async function submit() {
    setBusy(true); setErr('')
    try {
      await api.reportProblem(projectId, { subject_type: subject, subject_id: subjectId, note })
      setSent(true); setOpen(false); setNote('')
      onSent?.()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Request failed')
    } finally { setBusy(false) }
  }

  if (sent) {
    return <span className="badge neutral sm"><Icon.check size={11} />Reported for review</span>
  }
  if (!open) {
    return (
      <button className="btn ghost sm" onClick={() => setOpen(true)}>
        <Icon.flag />{label}
      </button>
    )
  }
  return (
    <div className="problem-form">
      <textarea
        autoFocus rows={2} value={note} maxLength={2000}
        placeholder="What is wrong?"
        onChange={e => setNote(e.target.value)}
      />
      {err && <div className="problem-err">{err}</div>}
      <div className="row" style={{ gap: 7, justifyContent: 'flex-end' }}>
        <button className="btn ghost sm" disabled={busy} onClick={() => { setOpen(false); setErr('') }}>
          Cancel
        </button>
        <button className="btn sm" disabled={busy || note.trim().length < 3} onClick={submit}>
          {busy ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  )
}
