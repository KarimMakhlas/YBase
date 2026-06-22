import React, { useEffect, useState } from 'react'
import { getInvite, joinWorkspace } from '../api.js'

export default function Join({ token, onJoined, onCancel }) {
  const [preview, setPreview] = useState({ loading: true })
  const [form, setForm] = useState({ display_name: '', email: '', password: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getInvite(token)
      .then((p) => { if (alive) setPreview({ loading: false, ...p }) })
      .catch(() => { if (alive) setPreview({ loading: false, valid: false, reason: 'not_found' }) })
    return () => { alive = false }
  }, [token])

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const user = await joinWorkspace({ token, ...form })
      onJoined(user)
    } catch (err) {
      setError(friendly(err.message))
    } finally {
      setBusy(false)
    }
  }

  if (preview.loading) {
    return (
      <div className="auth-page">
        <div className="auth-card"><div className="skeleton skel-row auth-loading" /></div>
      </div>
    )
  }

  if (!preview.valid) {
    const msg = {
      revoked: 'This invite has been revoked.',
      used: 'This invite has already been used.',
      expired: 'This invite has expired.',
    }[preview.reason] || 'This invite link is not valid.'
    return (
      <div className="auth-page">
        <div className="auth-card">
          <div className="auth-mark">YBase</div>
          <h1>Invite unavailable</h1>
          <p>{msg}</p>
          <button type="button" onClick={onCancel}>Go to sign in</button>
        </div>
      </div>
    )
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-mark">YBase</div>
        <h1>Join {preview.workspace_name}</h1>
        <p>
          You’ve been invited as <strong>{preview.role}</strong>. Create your account to join,
          or sign in with an existing account using the same email.
        </p>
        <label className="field">
          <span>Your name</span>
          <input value={form.display_name} onChange={set('display_name')} required />
        </label>
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            value={form.email}
            onChange={set('email')}
            placeholder={preview.email || ''}
            required
          />
        </label>
        <label className="field">
          <span>Password</span>
          <input
            type="password"
            value={form.password}
            onChange={set('password')}
            minLength={12}
            required
          />
        </label>
        {error && <div className="auth-error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy ? 'Joining…' : `Join ${preview.workspace_name}`}
        </button>
        <div className="auth-switch">
          <button type="button" className="linkbtn" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </div>
  )
}

function friendly(message) {
  if (/410/.test(message)) return 'This invite is no longer valid (used, revoked, or expired).'
  if (/401/.test(message)) return 'An account with this email exists — enter its password to join.'
  return message
}
