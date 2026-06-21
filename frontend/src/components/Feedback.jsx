import React, { useEffect, useMemo, useState } from 'react'
import { Quote } from 'lucide-react'
import { getAnswerFeedback, listAnswerFeedback, patchAnswerFeedback } from '../api.js'
import { formatDateTime as fmt } from '../format.js'
import { useToast } from './Toast.jsx'
import { Badge } from '../whybase/ui.jsx'
import PageHeader from '../whybase/PageHeader.jsx'

const STATUS_OPTIONS = [
  ['open', 'Open'],
  ['in_review', 'In review'],
  ['resolved', 'Resolved'],
  ['dismissed', 'Dismissed'],
  ['all', 'All statuses'],
]
const ISSUE_OPTIONS = [
  ['', 'Flagged issues'],
  ['wrong', 'Wrong answer'],
  ['missing_citation', 'Missing citation'],
  ['bad_citation', 'Bad citation'],
  ['outdated', 'Outdated'],
  ['not_in_memory', 'Not in memory'],
  ['other', 'Other'],
  ['helpful', 'Helpful'],
  ['all', 'All types'],
]
const ISSUE_LABELS = Object.fromEntries(ISSUE_OPTIONS.filter(([v]) => v))
const ISSUE_TONE = {
  wrong: 'danger', missing_citation: 'warning', bad_citation: 'warning',
  outdated: 'info', not_in_memory: 'neutral', other: 'neutral', helpful: 'success',
}
const STATUS_TONE = { open: 'warning', in_review: 'info', resolved: 'success', dismissed: 'neutral' }
const KIND_TONE = { decision: 'accent', question: 'warning', entity: 'info', topic: 'success' }

const issueLabel = (value) => ISSUE_LABELS[value] || value || 'Feedback'

function TraceNodes({ trace, onNavigate }) {
  const nodes = trace?.nodes || []
  if (!nodes.length) return <div className="md-empty">No trace nodes were saved.</div>
  return (
    <div className="trace-nodes">
      {nodes.map((n) => (
        <button key={n.id} className="trace-node" onClick={() => onNavigate && onNavigate('review', { nodeId: n.id })} title="Open in Review">
          <Badge tone={KIND_TONE[n.kind] || 'neutral'} variant="outline" mono>{n.kind}</Badge>
          <span>{n.label}</span>
          {n.status && <span className="trace-status tnum">{n.status}</span>}
        </button>
      ))}
    </div>
  )
}

export default function Feedback({ onOpenDoc, onNavigate }) {
  const [filters, setFilters] = useState({ status: 'open', issue_type: '' })
  const [items, setItems] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [resolutionNote, setResolutionNote] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const selected = useMemo(() => items?.find((item) => item.id === selectedId) || null, [items, selectedId])

  const loadItems = () =>
    listAnswerFeedback(filters)
      .then((list) => {
        setItems(list)
        setSelectedId((id) => (list.some((item) => item.id === id) ? id : list[0]?.id || null))
      })
      .catch((e) => toast(`Failed to load feedback: ${e.message}`))

  const loadDetail = (id = selectedId) => {
    if (!id) { setDetail(null); return }
    getAnswerFeedback(id)
      .then((fb) => { setDetail(fb); setResolutionNote(fb.resolution_note || '') })
      .catch((e) => toast(`Failed to load feedback detail: ${e.message}`))
  }

  useEffect(() => {
    const t = setTimeout(loadItems, 180)
    return () => clearTimeout(t)
  }, [filters]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { loadDetail(selectedId) }, [selectedId]) // eslint-disable-line react-hooks/exhaustive-deps

  const setStatusFilter = (e) => setFilters((f) => ({ ...f, status: e.target.value }))
  const setIssueFilter = (e) => {
    const issue_type = e.target.value
    setFilters((f) => ({ ...f, issue_type, status: issue_type === 'helpful' && f.status === 'open' ? 'all' : f.status }))
  }

  const updateStatus = async (status) => {
    if (!detail || busy) return
    setBusy(true)
    try {
      const updated = await patchAnswerFeedback(detail.id, { status, resolution_note: resolutionNote })
      setDetail(updated)
      setResolutionNote(updated.resolution_note || '')
      toast('Feedback updated', 'success')
      await loadItems()
    } catch (e) {
      toast(`Update failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app-page app-page--wide wb-reveal">
      <PageHeader
        kicker="Feedback"
        title={<>Turn flags into <em>fixes</em>.</>}
        lede="Every answer your team flagged — wrong, outdated, missing a citation, or spot on. Route each into Review or back to the source."
      />

      <div className="filters">
        <select className="wb-select" value={filters.status} onChange={setStatusFilter} aria-label="Feedback status">
          {STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select className="wb-select" value={filters.issue_type} onChange={setIssueFilter} aria-label="Issue type">
          {ISSUE_OPTIONS.map(([value, label]) => <option key={value || 'flagged'} value={value}>{label}</option>)}
        </select>
      </div>

      <div className="master-detail">
        <aside className="md-list">
          {!items && <div className="wb-skeleton" style={{ height: 80, borderRadius: 'var(--radius-md)' }} />}
          {items && items.length === 0 && <div className="md-empty">No feedback matches.</div>}
          {items && items.map((item) => (
            <button key={item.id} className={`review-item ${selectedId === item.id ? 'active' : ''}`} onClick={() => setSelectedId(item.id)}>
              <div className="review-item-top">
                <Badge tone={ISSUE_TONE[item.issue_type] || 'neutral'} variant="outline" mono>{issueLabel(item.issue_type)}</Badge>
                <Badge tone={STATUS_TONE[item.status] || 'neutral'} variant="soft" mono>{item.status.replace('_', ' ')}</Badge>
              </div>
              <b>{item.session_title}</b>
              <small>{item.note || item.answer_preview || 'No note provided'}</small>
              <span className="review-item-meta tnum">{item.reporter_name || item.reporter_email} · {fmt(item.updated_at)}</span>
            </button>
          ))}
        </aside>

        <section className="md-detail" key={selectedId}>
          {!selected && items && items.length === 0 && (
            <div className="md-empty">No feedback — try another filter or wait for members to flag answers.</div>
          )}
          {selected && !detail && <div className="wb-skeleton" style={{ height: 120, borderRadius: 'var(--radius-md)' }} />}
          {detail && (
            <>
              <div className="review-editor-head">
                <div className="review-editor-badges">
                  <Badge tone={ISSUE_TONE[detail.issue_type] || 'neutral'} variant="outline" mono>{issueLabel(detail.issue_type)}</Badge>
                  <Badge tone={STATUS_TONE[detail.status] || 'neutral'} variant="soft" mono dot>{detail.status.replace('_', ' ')}</Badge>
                  <span className="feedback-reporter tnum">Reported {fmt(detail.created_at)} by {detail.reporter_name || detail.reporter_email}</span>
                </div>
              </div>

              <div className="dlabel">Flagged question</div>
              <div className="feedback-session">{detail.session_title}</div>

              <div className="dlabel" style={{ marginTop: 'var(--sp-5)' }}>The answer that was flagged</div>
              <div className="feedback-answer">{detail.answer_text}</div>

              <div className="dlabel" style={{ marginTop: 'var(--sp-5)' }}>Member note</div>
              <div className="feedback-note">
                <Quote size={16} strokeWidth={1.8} />
                <p>{detail.note || 'No note provided.'}</p>
              </div>
              {detail.cited_chunk && (
                <button className="list-more" onClick={() => onOpenDoc && onOpenDoc(detail.cited_chunk.document_id, detail.cited_chunk.text)}>Open flagged citation →</button>
              )}

              <div className="review-subgrid" style={{ marginTop: 'var(--sp-5)' }}>
                <section>
                  <div className="dlabel">Citations</div>
                  {detail.citations.length === 0 && <div className="md-empty">No citations were saved.</div>}
                  {detail.citations.map((c) => (
                    <div className="evidence-card" key={c.chunk_id} style={{ cursor: 'pointer' }} onClick={() => onOpenDoc && onOpenDoc(c.document_id, c.text || c.snippet)} role="button" tabIndex={0}>
                      <div className="evidence-head">
                        <span className={`src-badge src-${c.source}`}><i className="src-dot" aria-hidden="true" />{c.source || 'source'}</span>
                        <span className="evidence-meta tnum">C{c.chunk_id} · {c.title || `Chunk ${c.chunk_id}`}{c.author ? ` · ${c.author}` : ''}</span>
                      </div>
                      <p className="evidence-snip">{c.snippet || c.text || 'No citation snippet saved.'}</p>
                    </div>
                  ))}
                </section>
                <section>
                  <div className="dlabel">Trace nodes</div>
                  <TraceNodes trace={detail.trace} onNavigate={onNavigate} />
                </section>
              </div>

              <div className="dlabel" style={{ marginTop: 'var(--sp-5)' }}>Resolution</div>
              <textarea
                className="wb-textarea"
                value={resolutionNote}
                onChange={(e) => setResolutionNote(e.target.value)}
                rows={2}
                placeholder="What changed, or why was this dismissed?"
              />
              {detail.resolved_at && (
                <div className="review-item-meta tnum" style={{ marginTop: 6 }}>Last resolved by {detail.resolved_by_name || 'admin'} at {fmt(detail.resolved_at)}</div>
              )}
              <div className="review-actions" style={{ marginTop: 'var(--sp-3)' }}>
                <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => updateStatus('in_review')} disabled={busy}>Mark in review</button>
                <button className="wb-btn wb-btn--primary wb-btn--sm" onClick={() => updateStatus('resolved')} disabled={busy}>Resolve</button>
                <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => updateStatus('dismissed')} disabled={busy}>Dismiss</button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
