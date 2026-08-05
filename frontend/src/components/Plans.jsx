import React, { useState } from 'react'
import { ArrowLeft, Check } from 'lucide-react'
import { billingCheckout } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

const TEAM_FEATURES = [
  'Unlimited team members',
  'Connect Slack, Jira, GitHub & more',
  'Memory formation across every source',
  'Ask-memory with cited answers',
  'Decision log, timeline & graph',
]

// status label → tone for the badge
const STATUS_TONE = { active: 'success', trialing: 'accent', expired: 'warning', past_due: 'warning' }

export default function Plans({ billing, canPay = false, onUpgraded, onBack }) {
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const status = billing?.plan_status || 'trialing'
  const isActive = status === 'active'

  const upgrade = async () => {
    if (busy || isActive) return
    setBusy(true)
    try {
      const res = await billingCheckout()
      // Stub returns {activated:true, url:null}. Once Stripe is wired, url will be
      // a Checkout URL to redirect to instead.
      if (res.url) { window.location.href = res.url; return }
      toast('You’re on the Team plan — editing re-enabled', 'success')
      onUpgraded?.()
    } catch (err) {
      // 501 = no payment provider wired up on this instance. That's a setup
      // state, not a failed payment, so say so plainly rather than "failed".
      if (/not configured/i.test(err.message)) {
        toast('Billing isn’t set up on this instance yet — contact your admin')
      } else {
        toast(`Upgrade failed: ${err.message}`)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app-page wb-reveal">
      {onBack && (
        <button className="linkbtn" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--text-secondary)' }} onClick={onBack}>
          <ArrowLeft size={14} strokeWidth={1.8} /> Back
        </button>
      )}
      <PageHeader
        kicker="Billing"
        title={<>Keep your memory <em>editable</em>.</>}
        lede="YBase is free for 7 days, no card required. Upgrade to Team to keep your workspace editable after the trial."
      />

      <div className="plan-card">
        <div className="plan-card-head">
          <div>
            <h2>Team</h2>
            <p className="settings-sub">Everything your team needs to remember why.</p>
          </div>
          <Badge tone={STATUS_TONE[status] || 'neutral'} variant="soft" mono>
            {isActive ? 'current plan' : status}
          </Badge>
        </div>
        <ul className="plan-features">
          {TEAM_FEATURES.map((f) => (
            <li key={f}><Check size={15} strokeWidth={2} /> {f}</li>
          ))}
        </ul>
        {isActive ? (
          <button className="wb-btn wb-btn--secondary" disabled>Current plan</button>
        ) : canPay ? (
          <button className="wb-btn wb-btn--primary wb-btn--lg" onClick={upgrade} disabled={busy}>
            {busy ? 'Activating…' : 'Upgrade to Team'}
          </button>
        ) : (
          <button className="wb-btn wb-btn--secondary" disabled title="Only the workspace owner can upgrade">
            Ask your workspace owner to upgrade
          </button>
        )}
      </div>
    </div>
  )
}
