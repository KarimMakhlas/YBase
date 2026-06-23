import React, { useEffect, useMemo, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { getJSON } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

const TYPE_LABEL = { document: 'doc', decision: 'decision', question: 'question' }
const TYPE_TONE = { decision: 'accent', question: 'warning', document: 'neutral' }

function monthOf(date) {
  if (!date) return 'Undated'
  const d = new Date(date + 'T00:00:00')
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

export default function Timeline({ focus, onOpenDoc }) {
  const [events, setEvents] = useState(null)
  const [failed, setFailed] = useState(false)
  const [type, setType] = useState('')
  const [source, setSource] = useState('')
  const [expanded, setExpanded] = useState(null)
  const toast = useToast()

  useEffect(() => {
    getJSON('/api/timeline')
      .then(setEvents)
      .catch((e) => { setFailed(true); toast(`Failed to load timeline: ${e.message}`) })
  }, [toast])

  useEffect(() => {
    if (!focus?.focusKey || !events) return
    setType(''); setSource('')
    setExpanded(focus.focusKey)
    requestAnimationFrame(() => {
      document.getElementById(`tl-${focus.focusKey}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [focus, events])

  const sources = useMemo(() => [...new Set((events || []).map((e) => e.source).filter(Boolean))].sort(), [events])

  const Header = () => (
    <PageHeader
      kicker="Timeline"
      title={<>The full story, <em>in order</em>.</>}
      lede="Every decision and question your team raised — in sequence, with revisits surfaced right where they happened."
    />
  )

  if (failed) {
    return <div className="app-page"><Header /><p style={{ color: 'var(--danger)' }}>Could not load the timeline. Is the backend running?</p></div>
  }

  if (!events) {
    return (
      <div className="app-page">
        <Header />
        <div style={{ marginTop: 'var(--sp-6)', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {[0, 1, 2, 3].map((i) => <div key={i} className="wb-skeleton" style={{ height: 64, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      </div>
    )
  }

  if (!events.length) {
    return (
      <div className="app-page">
        <Header />
        <div className="md-empty" style={{ marginTop: 'var(--sp-6)' }}>
          Nothing remembered yet — ingest documents from Slack, Notion, GitHub or Jira and they will appear here, alongside the decisions and questions extracted from them.
        </div>
      </div>
    )
  }

  const shown = events.filter((ev) => {
    if (type && ev.type !== type) return false
    if (source && ev.source !== source) return false
    return true
  })

  const items = []
  let lastMonth = null
  for (const ev of shown) {
    const m = monthOf(ev.date)
    if (m !== lastMonth) { items.push({ marker: m }); lastMonth = m }
    items.push({ ev })
  }

  return (
    <div className="app-page wb-reveal">
      <Header />
      <div className="filters">
        <select className="wb-select" value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by type">
          <option value="">All types</option>
          <option value="document">Documents</option>
          <option value="decision">Decisions</option>
          <option value="question">Questions</option>
        </select>
        <select className="wb-select" value={source} onChange={(e) => setSource(e.target.value)} aria-label="Filter by source">
          <option value="">All sources</option>
          {sources.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {shown.length === 0 && <p className="md-empty">Nothing matches these filters.</p>}

      {items.map((item, i) =>
        item.marker ? (
          <div key={`m-${item.marker}-${i}`} className="tl-marker">
            <b>{item.marker}</b>
            <span className="line" />
          </div>
        ) : (
          (() => {
            const ev = item.ev
            const key = `${ev.type}-${ev.id}`
            const isOpen = expanded === key
            const hasDetail = ev.summary || ev.author || (ev.people && ev.people.length > 0) || ev.type === 'document'
            const isLast = i === items.length - 1
            return (
              <div key={key} className="tl-item" id={`tl-${key}`} style={{ '--i': Math.min(i, 12) }}>
                <div className="tl-rail">
                  <span className={`tl-dot ${ev.type}`} />
                  {!isLast && <span className="tl-line" />}
                </div>
                <div
                  className="tl-body"
                  role="button"
                  tabIndex={0}
                  onClick={() => setExpanded(isOpen ? null : key)}
                  onKeyDown={(e) => e.key === 'Enter' && setExpanded(isOpen ? null : key)}
                  aria-expanded={isOpen}
                >
                  <div className="tl-head">
                    <span className="tl-date tnum">{ev.date || '—'}</span>
                    <Badge tone={TYPE_TONE[ev.type] || 'neutral'} variant="soft" mono>{TYPE_LABEL[ev.type]}</Badge>
                    {ev.source && <SrcBadge provider={ev.source}>{ev.source}</SrcBadge>}
                    {ev.status && <StatusBadge status={ev.status} />}
                    {ev.revisited && (
                      <Badge tone="info" variant="soft" mono title="A later decision revisits this one">
                        <RotateCcw size={11} strokeWidth={2} /> revisited
                      </Badge>
                    )}
                  </div>
                  <div className="tl-title">{ev.title}</div>
                  {isOpen && hasDetail && (
                    <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border-subtle)' }}>
                      {ev.summary && <div className="tl-sum">{ev.summary}</div>}
                      {ev.author && <div className="tnum" style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', color: 'var(--text-tertiary)', marginTop: 4 }}>by {ev.author}</div>}
                      {ev.people && ev.people.length > 0 && (
                        <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-tertiary)', marginTop: 4 }}>{ev.people.join(', ')}</div>
                      )}
                      {ev.type === 'document' && onOpenDoc && (
                        <button className="list-more" style={{ paddingTop: 8 }} onClick={(e) => { e.stopPropagation(); onOpenDoc(ev.id) }}>Open document →</button>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )
          })()
        )
      )}
    </div>
  )
}
