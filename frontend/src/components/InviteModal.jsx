import React, { useEffect, useState } from 'react'
import { X, Link2, Copy, Check } from 'lucide-react'
import { createWorkspaceInvite } from '../api.js'
import { useToast } from './Toast.jsx'

// Quick-invite popover reachable from the app shell (not just Settings). Sends
// an email invite when an address is given, otherwise generates a copyable link.
export default function InviteModal({ workspaceName, onClose, onNavigateSettings }) {
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [link, setLink] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const toast = useToast()

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const submit = async () => {
    if (busy) return
    setBusy(true)
    try {
      const res = await createWorkspaceInvite({ role, email: email.trim() || null })
      const url = `${window.location.origin}${res.path}`
      if (email.trim() && res.email?.status === 'sent') {
        toast(`Invite emailed to ${email.trim()}`, 'success')
        setEmail('')
      } else {
        setLink(url)
        try {
          await navigator.clipboard.writeText(url)
          setCopied(true)
          setTimeout(() => setCopied(false), 1600)
          toast('Invite link copied', 'success')
        } catch {
          /* clipboard blocked — link is shown below for manual copy */
        }
      }
    } catch (err) {
      toast(`Could not create invite: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="invite-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Invite teammates">
        <div className="invite-modal-head">
          <h3>Invite to {workspaceName}</h3>
          <button className="wb-iconbtn" onClick={onClose} aria-label="Close">
            <X size={17} strokeWidth={1.8} />
          </button>
        </div>
        <p className="settings-sub">
          Add an email to send an invite, or generate a link anyone can use to join as the chosen role.
        </p>
        <div className="invite-modal-form">
          <input
            className="wb-input"
            type="email"
            placeholder="teammate@company.com (optional)"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
          />
          <select
            className="wb-select"
            style={{ width: 120, flex: 'none' }}
            value={role}
            onChange={(e) => setRole(e.target.value)}
            aria-label="Invite role"
          >
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <button className="wb-btn wb-btn--primary" type="button" onClick={submit} disabled={busy}>
            <Link2 size={15} strokeWidth={1.8} /> {busy ? 'Working…' : email.trim() ? 'Send invite' : 'Create link'}
          </button>
        </div>
        {link && (
          <div className="invite-modal-link">
            <code>{link}</code>
            <button
              className="wb-btn wb-btn--sm"
              type="button"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(link)
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1600)
                } catch {
                  /* manual copy */
                }
              }}
            >
              {copied ? <Check size={14} strokeWidth={2.2} /> : <Copy size={14} strokeWidth={1.9} />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        )}
        {onNavigateSettings && (
          <button
            className="linkbtn invite-modal-more"
            type="button"
            onClick={() => { onClose(); onNavigateSettings() }}
          >
            Manage members & invites in Settings →
          </button>
        )}
      </div>
    </div>
  )
}
