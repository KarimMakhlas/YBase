import React, { useState } from 'react'
import { MailWarning, X } from 'lucide-react'
import { resendVerification } from '../api.js'
import { useToast } from './Toast.jsx'

// Shown while the signed-in account hasn't confirmed its email address.
// Verification doesn't gate sign-in (an instance with no email provider would
// lock everyone out), so this nudge is the only prompt the user gets.
export default function VerifyBanner({ email }) {
  const [hidden, setHidden] = useState(false)
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  if (hidden) return null

  const resend = async () => {
    if (busy) return
    setBusy(true)
    try {
      const res = await resendVerification()
      if (res.already_verified) toast('Already verified — thanks!', 'success')
      else if (res.sent) toast(`Verification email sent to ${email}`, 'success')
      // sent:false means the instance has no email provider configured; saying
      // "sent" would be a lie the user would wait on.
      else toast('Email isn’t configured on this instance — ask your admin to verify you')
    } catch (err) {
      toast(`Couldn’t resend: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="verify-banner" role="status">
      <MailWarning size={15} strokeWidth={1.9} />
      <span>
        Confirm <b>{email}</b> to secure your account.
      </span>
      <button className="linkbtn" onClick={resend} disabled={busy}>
        {busy ? 'Sending…' : 'Resend email'}
      </button>
      <button
        className="wb-iconbtn wb-iconbtn--sm verify-banner-close"
        onClick={() => setHidden(true)}
        title="Dismiss"
        aria-label="Dismiss"
      >
        <X size={14} strokeWidth={2} />
      </button>
    </div>
  )
}
