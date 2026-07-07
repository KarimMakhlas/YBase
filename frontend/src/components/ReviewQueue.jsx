import React, { useCallback, useEffect, useState } from 'react'
import { Check, X, Bot, GitMerge, CircleHelp, GitCommitHorizontal } from 'lucide-react'
import { listProposals, approveProposal, rejectProposal } from '../api.js'
import { useToast } from './Toast.jsx'
import { StatusBadge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

// Curator queue for agent write-back: proposals submitted via
// POST /api/agent/propose wait here — nothing an agent writes becomes live
// memory until an admin approves it (optionally correcting the wording first).

const TABS = [
  { id: 'pending', label: 'Pending' },
  { id: 'approved', label: 'Approved' },
  { id: 'rejected', label: 'Rejected' },
]

function age(iso) {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function ProposalCard({ p, onResolved, onNavigate }) {
  const toast = useToast()
  const [label, setLabel] = useState(p.label)
  const [summary, setSummary] = useState(p.summary)
  const [note, setNote] = useState('')
  const [rejecting, setRejecting] = useState(false)
  const [busy, setBusy] = useState(false)
  const edited = label !== p.label || summary !== p.summary

  const approve = async () => {
    setBusy(true)
    try {
      const body = {}
      if (label !== p.label) body.label = label
      if (summary !== p.summary) body.summary = summary
      const res = await approveProposal(p.id, body)
      toast('Approved — it’s memory now.')
      onResolved(p.id, res)
    } catch (e) {
      toast(`Approve failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const reject = async () => {
    setBusy(true)
    try {
      await rejectProposal(p.id, note.trim() || null)
      toast('Proposal rejected.')
      onResolved(p.id)
    } catch (e) {
      toast(`Reject failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="review-card wb-reveal">
      <div className="review-card-head">
        {p.kind === 'question'
          ? <CircleHelp size={15} strokeWidth={1.8} />
          : <GitCommitHorizontal size={15} strokeWidth={1.8} />}
        <input
          className="wb-input review-label"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          aria-label="Proposal title"
        />
        {p.status_suggestion && <StatusBadge status={p.status_suggestion} />}
      </div>
      <textarea
        className="wb-textarea review-summary"
        value={summary}
        onChange={(e) => setSummary(e.target.value)}
        rows={3}
        aria-label="Proposal summary"
      />
      <div className="review-meta">
        <span className="review-agent"><Bot size={13} strokeWidth={1.8} /> {p.key_name || 'agent'}</span>
        <span className="tnum">{age(p.created_at)}</span>
        {(p.topics || []).map((t) => (
          <button key={t} className="chip-q chip-q--sm" onClick={() => onNavigate('decisions', { topic: t })}>{t}</button>
        ))}
        {p.existing_node_id && (
          <span className="review-merge" title="An active node with this title already exists — approving merges into it.">
            <GitMerge size={13} strokeWidth={1.8} /> merges into existing
          </span>
        )}
      </div>
      {rejecting ? (
        <div className="review-actions">
          <input
            className="wb-input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why is this being rejected? (optional, shown to the agent)"
            autoFocus
          />
          <button className="wb-btn wb-btn--sm" disabled={busy} onClick={reject}>Reject</button>
          <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => setRejecting(false)}>Cancel</button>
        </div>
      ) : (
        <div className="review-actions">
          <button className="wb-btn wb-btn--primary wb-btn--sm" disabled={busy} onClick={approve}>
            <Check size={14} strokeWidth={2} /> Approve{edited ? ' with edits' : ''}
          </button>
          <button className="wb-btn wb-btn--ghost wb-btn--sm" disabled={busy} onClick={() => setRejecting(true)}>
            <X size={14} strokeWidth={2} /> Reject
          </button>
        </div>
      )}
    </div>
  )
}

export default function ReviewQueue({ onNavigate, onPendingChange }) {
  const [tab, setTab] = useState('pending')
  const [items, setItems] = useState(null)
  const toast = useToast()

  const load = useCallback((which) => {
    setItems(null)
    listProposals(which)
      .then((rows) => {
        setItems(rows)
        if (which === 'pending') onPendingChange?.(rows.length)
      })
      .catch((e) => toast(`Failed to load proposals: ${e.message}`))
  }, [toast, onPendingChange])

  useEffect(() => { load(tab) }, [tab, load])

  const onResolved = (id, res) => {
    setItems((rows) => {
      const next = (rows || []).filter((r) => r.id !== id)
      if (tab === 'pending') onPendingChange?.(next.length)
      return next
    })
    if (res?.node_id) onNavigate('decisions', { decisionId: res.node_id })
  }

  return (
    <div className="app-page app-page--wide wb-reveal">
      <PageHeader
        kicker="Review"
        title={<>Agents propose. <em>You decide.</em></>}
        lede="Decisions your AI agents want to record wait here — nothing becomes memory until you approve it."
      />
      <div className="review-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`leftnav-btn${tab === t.id ? ' is-active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {!items && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 'var(--sp-4)' }}>
          {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 120, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      )}
      {items && items.length === 0 && (
        <div className="md-empty" style={{ marginTop: 'var(--sp-4)' }}>
          {tab === 'pending'
            ? 'Queue is clear. Agent proposals (via the API or MCP propose_decision tool) land here.'
            : `No ${tab} proposals yet.`}
        </div>
      )}
      {items && items.length > 0 && (
        <div className="review-list">
          {tab === 'pending'
            ? items.map((p) => (
              <ProposalCard key={p.id} p={p} onResolved={onResolved} onNavigate={onNavigate} />
            ))
            : items.map((p) => (
              <div key={p.id} className="review-card review-card--done">
                <div className="review-card-head">
                  <StatusBadge status={p.status} />
                  <span className="review-done-label">{p.label}</span>
                </div>
                <div className="review-meta">
                  <span className="review-agent"><Bot size={13} strokeWidth={1.8} /> {p.key_name || 'agent'}</span>
                  {p.reviewed_by_email && <span>by {p.reviewed_by_email}</span>}
                  {p.reviewed_at && <span className="tnum">{age(p.reviewed_at)}</span>}
                  {p.resolution_note && <span className="review-note">“{p.resolution_note}”</span>}
                  {p.created_node_id && (
                    <button className="linkbtn" onClick={() => onNavigate('decisions', { decisionId: p.created_node_id })}>
                      view decision
                    </button>
                  )}
                </div>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}
