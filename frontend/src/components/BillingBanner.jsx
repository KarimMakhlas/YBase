import React from 'react'
import { AlertTriangle, Clock } from 'lucide-react'

// Thin strip under the topbar: a read-only warning once the trial ends, or a
// gentle days-left nudge during the trial. Hidden on the Plans page itself and
// for active (paid) workspaces.
export default function BillingBanner({ billing, activeTab, onUpgrade }) {
  if (!billing || activeTab === 'plans') return null

  if (billing.writable === false) {
    return (
      <div className="billing-banner billing-banner--warn" role="status">
        <AlertTriangle size={15} strokeWidth={1.9} />
        <span>
          This workspace is <b>read-only</b> — your trial has ended. Your data is safe;
          upgrade to keep editing.
        </span>
        <button className="wb-btn wb-btn--sm wb-btn--primary" onClick={onUpgrade}>
          Upgrade
        </button>
      </div>
    )
  }

  if (billing.plan_status === 'trialing' && billing.days_left != null) {
    const d = billing.days_left
    return (
      <div className="billing-banner billing-banner--trial" role="status">
        <Clock size={15} strokeWidth={1.9} />
        <span>
          {d === 0 ? 'Your free trial ends today.' : `${d} day${d === 1 ? '' : 's'} left in your free trial.`}
        </span>
        <button className="linkbtn" onClick={onUpgrade}>View plans →</button>
      </div>
    )
  }

  return null
}
