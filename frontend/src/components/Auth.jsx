import React, { useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { bootstrap, login, register } from '../api.js'

// view: 'bootstrap' (first run, locked) | 'login' | 'register'
export default function Auth({ mode, onAuthed, initialView, onBack }) {
  const [view, setView] = useState(mode === 'bootstrap' ? 'bootstrap' : initialView || 'login')
  const [form, setForm] = useState({
    workspace_name: '',
    display_name: '',
    email: '',
    password: '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const isBootstrap = view === 'bootstrap'
  const isRegister = view === 'register'
  const needsWorkspace = isBootstrap || isRegister

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      let user
      if (isBootstrap) {
        user = await bootstrap({
          workspace_name: form.workspace_name || 'Default Workspace',
          display_name: form.display_name,
          email: form.email,
          password: form.password,
        })
      } else if (isRegister) {
        user = await register({
          workspace_name: form.workspace_name,
          display_name: form.display_name,
          email: form.email,
          password: form.password,
        })
      } else {
        user = await login({ email: form.email, password: form.password })
      }
      onAuthed(user)
    } catch (err) {
      setError(friendly(err.message))
    } finally {
      setBusy(false)
    }
  }

  const heading = isBootstrap
    ? 'Create your workspace'
    : isRegister
      ? 'Create your workspace'
      : 'Sign in'
  const blurb = isBootstrap
    ? 'Set up the first owner account. Existing local memory will be assigned to this workspace.'
    : isRegister
      ? 'Start a new team memory. We’ll preload it with a short demo so you can ask a question right away.'
      : 'Use your team account to access workspace memory.'

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        {!isBootstrap && onBack && (
          <button type="button" className="linkbtn" style={{ alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--text-secondary)' }} onClick={onBack}>
            <ArrowLeft size={14} strokeWidth={1.8} /> Back to home
          </button>
        )}
        <div className="auth-mark">WhyBase</div>
        <h1>{heading}</h1>
        <p>{blurb}</p>
        {needsWorkspace && (
          <>
            <label className="field">
              <span>Workspace</span>
              <input
                value={form.workspace_name}
                onChange={set('workspace_name')}
                placeholder={isRegister ? 'Acme Engineering' : 'Default Workspace'}
                required={isBootstrap}
              />
            </label>
            <label className="field">
              <span>Your name</span>
              <input value={form.display_name} onChange={set('display_name')} required />
            </label>
          </>
        )}
        <label className="field">
          <span>Email</span>
          <input type="email" value={form.email} onChange={set('email')} required />
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
          {busy ? 'Working…' : isBootstrap ? 'Create workspace' : isRegister ? 'Create workspace' : 'Sign in'}
        </button>
        {!isBootstrap && (
          <div className="auth-switch">
            {isRegister ? (
              <span>
                Already have an account?{' '}
                <button type="button" className="linkbtn" onClick={() => { setView('login'); setError('') }}>
                  Sign in
                </button>
              </span>
            ) : (
              <span>
                New here?{' '}
                <button type="button" className="linkbtn" onClick={() => { setView('register'); setError('') }}>
                  Create a workspace
                </button>
              </span>
            )}
          </div>
        )}
      </form>
    </div>
  )
}

function friendly(message) {
  if (/409/.test(message)) return 'An account with this email already exists — sign in instead.'
  if (/403/.test(message)) return 'Public signup is disabled on this instance.'
  if (/401/.test(message)) return 'Invalid email or password.'
  return message
}
