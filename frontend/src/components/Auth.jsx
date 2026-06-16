import React, { useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { bootstrap, login, register, forgotPassword, resetPassword } from '../api.js'

// view: 'bootstrap' (first run, locked) | 'login' | 'register' | 'forgot' | 'reset'
export default function Auth({ mode, onAuthed, initialView, onBack, resetToken }) {
  const [view, setView] = useState(
    mode === 'bootstrap' ? 'bootstrap' : resetToken ? 'reset' : initialView || 'login'
  )
  const [form, setForm] = useState({
    workspace_name: '',
    display_name: '',
    email: '',
    password: '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('') // '' | 'forgot' | 'reset'

  const isBootstrap = view === 'bootstrap'
  const isRegister = view === 'register'
  const isForgot = view === 'forgot'
  const isReset = view === 'reset'
  // Public signup is identity-only — the workspace is named later in the setup
  // wizard. Only first-run bootstrap (self-hosted) names a workspace here.
  const showWorkspaceField = isBootstrap
  const showNameField = isBootstrap || isRegister

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))
  const goView = (v) => { setView(v); setError(''); setDone('') }

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (isForgot) {
        await forgotPassword(form.email)
        setDone('forgot')
        return
      }
      if (isReset) {
        await resetPassword(resetToken, form.password)
        setDone('reset')
        return
      }
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

  // Success panels (forgot link sent / password updated).
  if (done === 'forgot') {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <div className="auth-mark">WhyBase</div>
          <h1>Check your email</h1>
          <p>
            If an account exists for {form.email || 'that address'}, we’ve sent a
            link to reset your password. It expires shortly.
          </p>
          <button type="button" onClick={() => goView('login')}>Back to sign in</button>
        </div>
      </div>
    )
  }
  if (done === 'reset') {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <div className="auth-mark">WhyBase</div>
          <h1>Password updated</h1>
          <p>Your password has changed and other sessions were signed out. Sign in with your new password.</p>
          <button type="button" onClick={() => (onBack ? onBack() : goView('login'))}>Sign in</button>
        </div>
      </div>
    )
  }

  const heading = isBootstrap
    ? 'Create your workspace'
    : isRegister
      ? 'Create your account'
      : isForgot
        ? 'Reset your password'
        : isReset
          ? 'Choose a new password'
          : 'Sign in'
  const blurb = isBootstrap
    ? 'Set up the first owner account. Existing local memory will be assigned to this workspace.'
    : isRegister
      ? 'Start with your account — you’ll name your workspace and invite your team next.'
      : isForgot
        ? 'Enter your account email and we’ll send a link to reset your password.'
        : isReset
          ? 'Pick a new password (at least 12 characters). This signs out your other sessions.'
          : 'Use your team account to access workspace memory.'

  const showTopBack = !isBootstrap && !isForgot && !isReset && onBack

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        {showTopBack && (
          <button type="button" className="linkbtn" style={{ alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--text-secondary)' }} onClick={onBack}>
            <ArrowLeft size={14} strokeWidth={1.8} /> Back to home
          </button>
        )}
        <div className="auth-mark">WhyBase</div>
        <h1>{heading}</h1>
        <p>{blurb}</p>
        {showWorkspaceField && (
          <label className="field">
            <span>Workspace</span>
            <input
              value={form.workspace_name}
              onChange={set('workspace_name')}
              placeholder="Default Workspace"
              required={isBootstrap}
            />
          </label>
        )}
        {showNameField && (
          <label className="field">
            <span>Your name</span>
            <input value={form.display_name} onChange={set('display_name')} required />
          </label>
        )}
        {!isReset && (
          <label className="field">
            <span>Email</span>
            <input type="email" value={form.email} onChange={set('email')} required />
          </label>
        )}
        {!isForgot && (
          <label className="field">
            <span>{isReset ? 'New password' : 'Password'}</span>
            <input
              type="password"
              value={form.password}
              onChange={set('password')}
              minLength={12}
              required
            />
          </label>
        )}
        {error && <div className="auth-error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy
            ? 'Working…'
            : isBootstrap
              ? 'Create workspace'
              : isRegister
                ? 'Create account'
              : isForgot
                ? 'Send reset link'
                : isReset
                  ? 'Update password'
                  : 'Sign in'}
        </button>
        {view === 'login' && (
          <div className="auth-switch">
            <span>
              New here?{' '}
              <button type="button" className="linkbtn" onClick={() => goView('register')}>
                Create a workspace
              </button>
            </span>
            <div style={{ marginTop: 6 }}>
              <button type="button" className="linkbtn" onClick={() => goView('forgot')}>
                Forgot password?
              </button>
            </div>
          </div>
        )}
        {isRegister && (
          <div className="auth-switch">
            <span>
              Already have an account?{' '}
              <button type="button" className="linkbtn" onClick={() => goView('login')}>
                Sign in
              </button>
            </span>
          </div>
        )}
        {isForgot && (
          <div className="auth-switch">
            <button type="button" className="linkbtn" onClick={() => goView('login')}>
              Back to sign in
            </button>
          </div>
        )}
        {isReset && (
          <div className="auth-switch">
            <button type="button" className="linkbtn" onClick={() => (onBack ? onBack() : goView('login'))}>
              Back to sign in
            </button>
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
  if (/429/.test(message)) return 'Too many attempts — please wait a minute and try again.'
  if (/400/.test(message)) return 'This reset link is invalid or has expired.'
  return message
}
