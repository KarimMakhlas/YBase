import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity, AlertTriangle, Bot, Gauge, GitCommitHorizontal, Inbox, Layers, Plug, Zap,
} from 'lucide-react'
import { getOpsFleet, getOpsActivity } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

// Fleet view: one card of operational vitals per workspace the current user
// operates (admin/owner), plus a merged audit feed. Client-side thresholds
// only — no pager wiring in v1. Polls every 30s while the page is visible.

const POLL_MS = 30000

const queueTone = (depth) => (depth > 50 ? 'danger' : depth > 25 ? 'warning' : 'success')

function fmtMs(ms) {
  if (ms == null) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function fmtTokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

function age(iso) {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function Vital({ Icon, label, value, tone }) {
  return (
    <div className="ops-vital">
      <span className="ops-vital-label"><Icon size={13} strokeWidth={1.8} /> {label}</span>
      {tone
        ? <Badge tone={tone} variant="soft" mono>{value}</Badge>
        : <b className="tnum">{value}</b>}
    </div>
  )
}

function WorkspaceCard({ w, onNavigate }) {
  const alerts = []
  if (w.failing_connectors.length) alerts.push(`${w.failing_connectors.join(', ')} sync failing`)
  if (w.failed_docs > 0) alerts.push(`${w.failed_docs} docs failed formation`)
  if (w.slo_24h.failures > 0) alerts.push(`${w.slo_24h.failures} formation failures (24h)`)

  return (
    <div className={`ops-card${alerts.length ? ' ops-card--alerting' : ''}`}>
      <div className="ops-card-head">
        <b>{w.name}</b>
        {w.is_active && <Badge tone="info" variant="soft" mono>active</Badge>}
        <span className="ops-card-role">{w.role}</span>
      </div>
      <div className="ops-vitals">
        <Vital Icon={Layers} label="Queue" value={w.queue_depth} tone={queueTone(w.queue_depth)} />
        <Vital
          Icon={Inbox} label="Proposals" value={w.pending_proposals}
          tone={w.pending_proposals > 0 ? 'warning' : 'success'}
        />
        <Vital
          Icon={Plug} label="Connectors"
          value={w.connectors.length
            ? `${w.connectors.length - w.failing_connectors.length}/${w.connectors.length} ok`
            : 'none'}
          tone={w.connectors.length ? (w.failing_connectors.length ? 'danger' : 'success') : undefined}
        />
        <Vital Icon={Gauge} label="p95 formation" value={fmtMs(w.slo_24h.p95_ms)} />
        <Vital Icon={Zap} label="Tokens 24h" value={fmtTokens(w.tokens_24h)} />
        <Vital Icon={GitCommitHorizontal} label="Decisions" value={w.decisions} />
      </div>
      {alerts.length > 0 && (
        <div className="ops-alerts">
          {alerts.map((a) => (
            <span key={a} className="ops-alert"><AlertTriangle size={12} strokeWidth={1.9} /> {a}</span>
          ))}
        </div>
      )}
      {w.is_active && w.pending_proposals > 0 && (
        <button className="linkbtn" onClick={() => onNavigate('review')}>
          review {w.pending_proposals} pending proposal{w.pending_proposals > 1 ? 's' : ''} →
        </button>
      )}
    </div>
  )
}

export default function OpsDashboard({ onNavigate }) {
  const [fleet, setFleet] = useState(null)
  const [events, setEvents] = useState(null)
  const [wsFilter, setWsFilter] = useState(null)
  const toast = useToast()
  const failedOnce = useRef(false)

  const load = useCallback(async (filter) => {
    try {
      const [f, a] = await Promise.all([getOpsFleet(), getOpsActivity(filter)])
      setFleet(f.workspaces)
      setEvents(a.events)
      failedOnce.current = false
    } catch (e) {
      if (!failedOnce.current) toast(`Failed to load fleet: ${e.message}`)
      failedOnce.current = true
    }
  }, [toast])

  useEffect(() => {
    load(wsFilter)
    const tick = setInterval(() => {
      if (document.visibilityState === 'visible') load(wsFilter)
    }, POLL_MS)
    return () => clearInterval(tick)
  }, [load, wsFilter])

  return (
    <div className="app-page app-page--wide wb-reveal">
      <PageHeader
        kicker="Ops"
        title={<>Every workspace, <em>one glance.</em></>}
        lede="Queues, sync health, agent proposals, and formation latency across everything you operate. Refreshes every 30 seconds."
      />
      {!fleet && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 'var(--sp-4)' }}>
          {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 130, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      )}
      {fleet && fleet.length === 0 && (
        <div className="md-empty" style={{ marginTop: 'var(--sp-4)' }}>
          You don’t operate any workspaces — the fleet view covers workspaces where you’re an admin or owner.
        </div>
      )}
      {fleet && fleet.length > 0 && (
        <div className="ops-fleet">
          {fleet.map((w) => <WorkspaceCard key={w.workspace_id} w={w} onNavigate={onNavigate} />)}
        </div>
      )}

      <div className="dlabel" style={{ marginTop: 'var(--sp-6)', display: 'flex', alignItems: 'center', gap: 10 }}>
        <span><Activity size={13} strokeWidth={1.9} style={{ verticalAlign: -2 }} /> Recent activity</span>
        {fleet && fleet.length > 1 && (
          <select
            className="wb-select"
            style={{ width: 'auto', padding: '3px 26px 3px 8px', fontSize: 'var(--fs-xs)' }}
            value={wsFilter ?? ''}
            onChange={(e) => setWsFilter(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">All workspaces</option>
            {fleet.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.name}</option>)}
          </select>
        )}
      </div>
      {!events && <div className="wb-skeleton" style={{ height: 80, borderRadius: 'var(--radius-md)', marginTop: 8 }} />}
      {events && events.length === 0 && <div className="md-empty">No activity yet.</div>}
      {events && events.length > 0 && (
        <div className="ops-feed">
          {events.map((e) => (
            <div className="ops-feed-row" key={e.id}>
              <span className="ops-feed-actor">
                {e.actor || <><Bot size={12} strokeWidth={1.8} /> system</>}
              </span>
              <span className="ops-feed-action">{e.action.replaceAll('_', ' ')}</span>
              {fleet && fleet.length > 1 && <span className="ops-feed-ws">{e.workspace_name}</span>}
              <span className="ops-feed-time tnum">{age(e.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
