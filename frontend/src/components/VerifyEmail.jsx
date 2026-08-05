import React, { useEffect, useState } from 'react'
import { MailCheck, MailX } from 'lucide-react'
import { verifyEmail } from '../api.js'

// Landing page for #/verify/<token>. Runs the POST once on mount and reports
// the outcome. Works signed-in or signed-out — the token itself is the proof,
// so there's nothing to gate on a session.
export default function VerifyEmail({ token, onDone }) {
  const [state, setState] = useState({ status: 'working', message: '' })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await verifyEmail(token)
        if (cancelled) return
        setState({
          status: 'ok',
          message: res.already_verified
            ? 'This address was already verified — you’re all set.'
            : 'Your email address is verified.',
        })
      } catch (err) {
        if (cancelled) return
        setState({ status: 'error', message: err.message })
      }
    })()
    return () => { cancelled = true }
  }, [token])

  const Icon = state.status === 'error' ? MailX : MailCheck

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="verify-result">
          {state.status !== 'working' && (
            <Icon
              size={30}
              strokeWidth={1.7}
              className={state.status === 'error' ? 'verify-icon is-error' : 'verify-icon'}
            />
          )}
          <h1>
            {state.status === 'working' ? 'Verifying…'
              : state.status === 'ok' ? 'Email verified' : 'Couldn’t verify'}
          </h1>
          {state.message && <p className="settings-sub">{state.message}</p>}
          {state.status === 'error' && (
            <p className="settings-sub">
              Verification links expire. Sign in and use “Resend” to get a fresh one.
            </p>
          )}
          {state.status !== 'working' && (
            <button className="wb-btn wb-btn--primary" onClick={onDone}>Continue</button>
          )}
        </div>
      </div>
    </div>
  )
}
