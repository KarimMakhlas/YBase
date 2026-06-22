import React, { useEffect, useMemo, useState } from 'react'
import { Search, ChevronRight, ArrowRight, Link2, Download } from 'lucide-react'
import { createDecisionShare, getJSON, revokeDecisionShare } from '../api.js'
import { decisionsToCSV, decisionsToMarkdown, downloadFile } from '../export.js'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

const REL_LABEL = {
  revisits: { out: 'revisits', in: 'revisited by' },
  resolves: { out: 'resolves', in: 'resolved by' },
  relates_to: { out: 'related to', in: 'related to' },
}

// Per-decision public share link: create (idempotent), copy, revoke.
function ShareControl({ decision }) {
  const [share, setShare] = useState(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const fullUrl = share ? `${window.location.origin}${share.path}` : ''

  const ensure = async () => {
    if (busy) return
    setBusy(true)
    try {
      setShare(await createDecisionShare(decision.id))
      setOpen(true)
    } catch (e) {
      toast(`Could not create link: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(fullUrl)
      toast('Share link copied', 'success')
    } catch {
      toast('Copy failed — select and copy manually')
    }
  }

  const revoke = async () => {
    try {
      await revokeDecisionShare(decision.id)
      setShare(null)
      setOpen(false)
      toast('Share link revoked', 'success')
    } catch (e) {
      toast(`Revoke failed: ${e.message}`)
    }
  }

  if (!open) {
    return (
      <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={ensure} disabled={busy}>
        <Link2 size={14} strokeWidth={1.8} /> {busy ? 'Creating…' : 'Share'}
      </button>
    )
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
      <input
        className="wb-input wb-input--sm"
        style={{ flex: 1, minWidth: 240 }}
        readOnly
        value={fullUrl}
        onFocus={(e) => e.target.select()}
        aria-label="Public share link"
      />
      <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={copy}>Copy</button>
      <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={revoke}>Revoke</button>
      {share?.view_count > 0 && <span className="tnum" style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-tertiary)' }}>{share.view_count} views</span>}
    </div>
  )
}

// Oldest → newest: decisions this one revisits come before it, those that revisit it come after.
function RevisitChain({ decision }) {
  const older = decision.related.filter((r) => r.relation === 'revisits' && r.direction === 'out')
  const newer = decision.related.filter((r) => r.relation === 'revisits' && r.direction === 'in')
  if (!older.length && !newer.length) return null

  const node = (label, status, date, current, key) => (
    <div key={key} className={`chain-node ${current ? 'current' : ''}`}>
      <b>{label}</b>
      <span>{[status, date].filter(Boolean).join(' · ')}</span>
    </div>
  )
  const arrow = (key) => (
    <span key={key} className="chain-arrow" aria-hidden="true"><ArrowRight size={15} strokeWidth={1.8} /></span>
  )

  const parts = []
  older.forEach((r, i) => { parts.push(node(r.label, r.status, r.date, false, `o-${i}`)); parts.push(arrow(`oa-${i}`)) })
  parts.push(node(decision.title, decision.status, decision.date, true, 'cur'))
  newer.forEach((r, i) => { parts.push(arrow(`na-${i}`)); parts.push(node(r.label, r.status, r.date, false, `n-${i}`)) })

  return (
    <>
      <div className="dlabel" style={{ marginTop: 'var(--sp-4)' }}>Decision history</div>
      <div className="chain">{parts}</div>
    </>
  )
}

export default function Decisions({ focus, onOpenDoc }) {
  const [decisions, setDecisions] = useState(null)
  const [failed, setFailed] = useState(false)
  const [topic, setTopic] = useState('')
  const [person, setPerson] = useState('')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState('newest')
  const [open, setOpen] = useState(null)
  const toast = useToast()

  useEffect(() => {
    getJSON('/api/decisions')
      .then(setDecisions)
      .catch((e) => { setFailed(true); toast(`Failed to load decisions: ${e.message}`) })
  }, [toast])

  useEffect(() => {
    if (!focus) return
    if (focus.decisionId != null) {
      setTopic(''); setPerson(''); setSearch('')
      setOpen(focus.decisionId)
      requestAnimationFrame(() => {
        document.getElementById(`decision-${focus.decisionId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      })
    }
    if (focus.topic) setTopic(focus.topic)
  }, [focus, decisions])

  const topics = useMemo(() => [...new Set((decisions || []).flatMap((d) => d.topics))].sort(), [decisions])
  const people = useMemo(() => [...new Set((decisions || []).flatMap((d) => d.people.concat(d.made_by)))].sort(), [decisions])

  const Header = () => (
    <PageHeader
      kicker="Decision log"
      title={<>Every call, <em>on the record</em>.</>}
      lede="What you decided, the reasoning at the time, who pushed for what — and every time it was revisited."
    />
  )

  if (failed) {
    return (
      <div className="app-page">
        <Header />
        <p style={{ color: 'var(--danger)' }}>Could not load decisions. Is the backend running?</p>
      </div>
    )
  }

  if (!decisions) {
    return (
      <div className="app-page">
        <Header />
        <div style={{ marginTop: 'var(--sp-5)', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {[0, 1, 2].map((i) => <div key={i} className="wb-skeleton" style={{ height: 58, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      </div>
    )
  }

  if (!decisions.length) {
    return (
      <div className="app-page">
        <Header />
        <div className="md-empty" style={{ marginTop: 'var(--sp-6)' }}>
          No decisions tracked yet — once documents are ingested, memory formation extracts the decisions inside them, with reasoning, advocates, and alternatives considered.
        </div>
      </div>
    )
  }

  const shown = decisions
    .filter((d) => {
      if (topic && !d.topics.includes(topic)) return false
      if (person && !d.people.includes(person) && !d.made_by.includes(person)) return false
      if (search) {
        const hay = `${d.title} ${d.summary || ''}`.toLowerCase()
        if (!hay.includes(search.toLowerCase())) return false
      }
      return true
    })
    .sort((a, b) => {
      const da = a.date || ''
      const db = b.date || ''
      return sort === 'oldest' ? da.localeCompare(db) : db.localeCompare(da)
    })

  return (
    <div className="app-page wb-reveal">
      <Header />
      <div className="filters">
        <select className="wb-select" value={topic} onChange={(e) => setTopic(e.target.value)} aria-label="Filter by topic">
          <option value="">All topics</option>
          {topics.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select className="wb-select" value={person} onChange={(e) => setPerson(e.target.value)} aria-label="Filter by person">
          <option value="">All people</option>
          {people.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select className="wb-select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort order">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
        <div className="wb-input-wrap">
          <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Search size={16} strokeWidth={1.8} /></span>
          <input className="wb-input wb-input--has-prefix" placeholder="Search decisions" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => downloadFile('decisions.csv', decisionsToCSV(shown), 'text/csv')} disabled={!shown.length}>
            <Download size={14} strokeWidth={1.8} /> CSV
          </button>
          <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => downloadFile('decisions.md', decisionsToMarkdown(shown), 'text/markdown')} disabled={!shown.length}>
            <Download size={14} strokeWidth={1.8} /> Markdown
          </button>
        </div>
      </div>

      {shown.length === 0 && <p className="md-empty">No decisions match.</p>}

      {shown.map((d, i) => {
        const isOpen = open === d.id
        return (
          <article className={`decision ${isOpen ? 'open' : ''}`} key={d.id} id={`decision-${d.id}`} style={{ '--i': Math.min(i, 12) }}>
            <button className="decision-toggle" onClick={() => setOpen(isOpen ? null : d.id)} aria-expanded={isOpen}>
              <StatusBadge status={d.status} />
              <h3>{d.title}</h3>
              <span className="tl-date tnum">{d.date || 'undated'}</span>
              {typeof d.confidence === 'number' && (
                <>
                  <span className="tnum" style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-tertiary)' }}>{Math.round(d.confidence * 100)}%</span>
                  <span className="conf-meter" title={`confidence ${Math.round(d.confidence * 100)}% — status, recency, and evidence combined`}>
                    <i style={{ width: `${Math.round(d.confidence * 100)}%` }} />
                  </span>
                </>
              )}
              <ChevronRight size={17} strokeWidth={1.8} className="decision-chev" />
            </button>
            {isOpen && (
              <div className="decision-body wb-reveal">
                {d.summary && <p>{d.summary}</p>}
                {d.positions.length > 0 && (
                  <>
                    <div className="dlabel">Positions on the record</div>
                    <div className="positions">
                      {d.positions.map((p, j) => (
                        <div className="position" key={j}><span className="pos">{p}</span></div>
                      ))}
                    </div>
                  </>
                )}
                {d.alternatives_considered.length > 0 && (
                  <>
                    <div className="dlabel">Alternatives considered</div>
                    <p style={{ marginTop: -4 }}>{d.alternatives_considered.join(' · ')}</p>
                  </>
                )}
                {d.made_by.length > 0 && (
                  <>
                    <div className="dlabel">People</div>
                    <p style={{ marginTop: -4 }}>{d.made_by.join(', ')}</p>
                  </>
                )}
                {d.topics.length > 0 && (
                  <div className="topics">
                    {d.topics.map((t) => (
                      <button key={t} className="wb-tag wb-tag--button" onClick={() => setTopic(t)}>{t}</button>
                    ))}
                  </div>
                )}
                <RevisitChain decision={d} />
                {d.related.filter((r) => r.relation !== 'revisits').length > 0 && (
                  <>
                    <div className="dlabel" style={{ marginTop: 'var(--sp-4)' }}>Graph</div>
                    <p style={{ marginTop: -4 }}>
                      {d.related.filter((r) => r.relation !== 'revisits').map((r, j) => (
                        <span key={j} style={{ marginRight: 10 }}>
                          {(REL_LABEL[r.relation] || {})[r.direction] || r.relation}{' '}
                          <em style={{ fontStyle: 'normal', color: 'var(--text)', fontWeight: 'var(--fw-medium)' }}>{r.label}</em>
                        </span>
                      ))}
                    </p>
                  </>
                )}
                {d.sources.length > 0 && (
                  <div className="dsources">
                    {d.sources.map((s) => (
                      <button
                        key={s.document_id}
                        className={`src-badge src-${s.source}`}
                        style={{ cursor: 'pointer' }}
                        title="Open the source document"
                        onClick={() => onOpenDoc && onOpenDoc(s.document_id)}
                      >
                        <i className="src-dot" aria-hidden="true" /> {s.source}: {s.title} ({s.date || '—'})
                      </button>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: 'var(--sp-4)' }}>
                  <ShareControl decision={d} />
                </div>
              </div>
            )}
          </article>
        )
      })}
    </div>
  )
}
