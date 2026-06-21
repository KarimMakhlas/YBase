import React, { useEffect, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { bootstrap, login, register, forgotPassword, resetPassword, getAuthProviders } from '../api.js'

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
  const [googleOn, setGoogleOn] = useState(false)

  // Discover configured third-party providers, and surface a failed Google
  // round-trip (the callback redirects back with ?auth_error=google).
  useEffect(() => {
    getAuthProviders().then((p) => setGoogleOn(!!p.google)).catch(() => {})
    if (/[?&]auth_error=google/.test(window.location.search)) {
      setError('Google sign-in didn’t complete. Please try again.')
    }
  }, [])

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
          <div className="auth-mark">YBase</div>
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
          <div className="auth-mark">YBase</div>
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
        <div className="auth-mark">YBase</div>
        <h1>{heading}</h1>
        <p>{blurb}</p>
        {googleOn && (view === 'login' || isRegister) && (
          <>
            <button
              type="button"
              className="auth-google"
              onClick={() => { window.location.href = '/api/auth/google/start' }}
            >
              <GoogleG /> Continue with Google
            </button>
            <div className="auth-or"><span>or</span></div>
          </>
        )}
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

function GoogleG() {
  return (
    <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
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
