import React, { useEffect, useMemo, useState } from 'react'
import { Search, Archive, TriangleAlert } from 'lucide-react'
import {
  archiveReviewNode, getReviewNode, listReviewNodes, patchReviewNode, unarchiveReviewNode,
} from '../api.js'
import { formatDateTime as fmt } from '../format.js'
import { useToast } from './Toast.jsx'
import { Badge } from '../whybase/ui.jsx'
import PageHeader from '../whybase/PageHeader.jsx'

const KINDS = ['', 'decision', 'question', 'entity', 'topic']
const STATES = [
  ['needs_review', 'Needs review'],
  ['reviewed', 'Reviewed'],
  ['archived', 'Archived'],
  ['all', 'All'],
]
const STATUSES = {
  decision: ['', 'decided', 'proposed', 'revisited', 'reversed', 'reaffirmed'],
  question: ['', 'open', 'resolved'],
  entity: [''],
  topic: [''],
}
const KIND_TONE = { decision: 'accent', question: 'warning', entity: 'info', topic: 'success' }
const STATE_TONE = { 'needs review': 'warning', reviewed: 'success', archived: 'neutral' }

function nodeState(n) {
  if (n.archived_at) return 'archived'
  if (n.curated_at) return 'reviewed'
  return 'needs review'
}

function parseData(text) {
  const parsed = JSON.parse(text || '{}')
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Data must be a JSON object')
  return parsed
}

export default function Review({ focus = null }) {
  const [filters, setFilters] = useState({ state: 'needs_review', kind: '', q: '' })
  const [nodes, setNodes] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [form, setForm] = useState({ label: '', summary: '', status: '', data: '{}' })
  const [jsonError, setJsonError] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const selected = useMemo(() => nodes?.find((n) => n.id === selectedId) || null, [nodes, selectedId])

  const loadNodes = () =>
    listReviewNodes(filters)
      .then((list) => {
        setNodes(list)
        setSelectedId((id) => (list.some((n) => n.id === id) ? id : list[0]?.id || null))
      })
      .catch((e) => toast(`Failed to load review queue: ${e.message}`))

  const loadDetail = (id = selectedId) => {
    if (!id) { setDetail(null); return }
    getReviewNode(id)
      .then((node) => {
        setDetail(node)
        setForm({ label: node.label || '', summary: node.summary || '', status: node.status || '', data: JSON.stringify(node.data || {}, null, 2) })
        setJsonError('')
      })
      .catch((e) => toast(`Failed to load memory node: ${e.message}`))
  }

  useEffect(() => {
    const t = setTimeout(loadNodes, 220)
    return () => clearTimeout(t)
  }, [filters]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { loadDetail(selectedId) }, [selectedId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!focus?.nodeId) return
    setFilters({ state: 'all', kind: '', q: '' })
    setSelectedId(focus.nodeId)
  }, [focus?.n, focus?.nodeId])

  const setFilter = (key) => (e) => setFilters((f) => ({ ...f, [key]: e.target.value }))
  const set = (key) => (e) => {
    setForm((f) => ({ ...f, [key]: e.target.value }))
    if (key === 'data') setJsonError('')
  }

  const refreshAfterChange = async (node) => {
    setDetail(node)
    setForm({ label: node.label || '', summary: node.summary || '', status: node.status || '', data: JSON.stringify(node.data || {}, null, 2) })
    await loadNodes()
  }

  const save = async () => {
    if (!detail || busy) return
    let data
    try { data = parseData(form.data) } catch (e) { setJsonError(e.message); return }
    setBusy(true)
    try {
      const node = await patchReviewNode(detail.id, {
        label: form.label.trim(),
        summary: form.summary.trim() || null,
        status: form.status || null,
        data,
        mark_reviewed: true,
      })
      toast('Memory updated', 'success')
      await refreshAfterChange(node)
    } catch (e) {
      toast(`Save failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const markReviewed = async () => {
    if (!detail || busy) return
    setBusy(true)
    try {
      const node = await patchReviewNode(detail.id, { mark_reviewed: true })
      toast('Marked reviewed', 'success')
      await refreshAfterChange(node)
    } catch (e) {
      toast(`Review failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const archive = async () => {
    if (!detail || busy) return
    const reason = window.prompt('Archive reason', detail.archive_reason || '')
    if (reason === null) return
    setBusy(true)
    try {
      const node = await archiveReviewNode(detail.id, reason)
      toast('Memory archived', 'success')
      await refreshAfterChange(node)
    } catch (e) {
      toast(`Archive failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const unarchive = async () => {
    if (!detail || busy) return
    setBusy(true)
    try {
      const node = await unarchiveReviewNode(detail.id)
      toast('Memory restored', 'success')
      await refreshAfterChange(node)
    } catch (e) {
      toast(`Restore failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const statusOptions = STATUSES[detail?.kind] || ['']

  return (
    <div className="app-page app-page--wide wb-reveal">
      <PageHeader
        kicker="Review"
        title={<>Approve what becomes <em>truth</em>.</>}
        lede="Curate extracted memory before it's trusted. Edit the claim, fix its status, or archive what doesn't belong."
      />

      <div className="filters">
        <select className="wb-select" value={filters.state} onChange={setFilter('state')} aria-label="Review state">
          {STATES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select className="wb-select" value={filters.kind} onChange={setFilter('kind')} aria-label="Memory kind">
          {KINDS.map((k) => <option key={k || 'all'} value={k}>{k || 'All kinds'}</option>)}
        </select>
        <div className="wb-input-wrap">
          <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Search size={16} strokeWidth={1.8} /></span>
          <input className="wb-input wb-input--has-prefix" value={filters.q} onChange={setFilter('q')} placeholder="Search memory" aria-label="Search memory" />
        </div>
      </div>

      <div className="master-detail">
        <aside className="md-list">
          {!nodes && <div className="wb-skeleton" style={{ height: 80, borderRadius: 'var(--radius-md)' }} />}
          {nodes && nodes.length === 0 && <div className="md-empty">No memory nodes match.</div>}
          {nodes && nodes.map((n) => (
            <button key={n.id} className={`review-item ${selectedId === n.id ? 'active' : ''}`} onClick={() => setSelectedId(n.id)}>
              <div className="review-item-top">
                <Badge tone={KIND_TONE[n.kind] || 'neutral'} variant="outline" mono>{n.kind}</Badge>
                <Badge tone={STATE_TONE[nodeState(n)] || 'neutral'} variant="soft" mono>{nodeState(n)}</Badge>
              </div>
              <b>{n.label}</b>
              <small>{n.summary || 'No summary'}</small>
              <span className="review-item-meta tnum">{n.evidence_count} source links · {n.neighbor_count} graph links</span>
            </button>
          ))}
        </aside>

        <section className="md-detail review-editor" key={selectedId}>
          {!selected && nodes && nodes.length === 0 && (
            <div className="md-empty">Nothing to review — try another filter or ingest more documents.</div>
          )}
          {selected && !detail && <div className="wb-skeleton" style={{ height: 120, borderRadius: 'var(--radius-md)' }} />}
          {detail && (
            <>
              <div className="review-editor-head">
                <div className="review-editor-badges">
                  <Badge tone={KIND_TONE[detail.kind] || 'neutral'} variant="outline" mono>{detail.kind}</Badge>
                  <Badge tone={STATE_TONE[nodeState(detail)] || 'neutral'} variant="soft" mono dot>{nodeState(detail)}</Badge>
                  <span className="review-item-meta tnum">
                    updated {fmt(detail.updated_at)}
                    {detail.curated_at ? ` · reviewed ${fmt(detail.curated_at)}` : ''}
                    {detail.archived_at ? ` · archived ${fmt(detail.archived_at)}` : ''}
                  </span>
                </div>
                <div className="review-actions">
                  <button className="wb-btn wb-btn--primary wb-btn--sm" onClick={save} disabled={busy || !form.label.trim()}>Save &amp; review</button>
                  <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={markReviewed} disabled={busy || !!detail.archived_at}>Mark reviewed</button>
                  {detail.archived_at
                    ? <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={unarchive} disabled={busy}>Unarchive</button>
                    : <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={archive} disabled={busy}>Archive</button>}
                </div>
              </div>

              {detail.archive_reason && <div className="source-alert"><Archive size={16} strokeWidth={1.8} /> Archive reason: {detail.archive_reason}</div>}

              <div className="review-grid">
                <label className="rfield rfield--wide">
                  <span>Label</span>
                  <input className="wb-input" value={form.label} onChange={set('label')} />
                </label>
                <label className="rfield">
                  <span>Status</span>
                  <select className="wb-select" value={form.status} onChange={set('status')} disabled={statusOptions.length === 1}>
                    {statusOptions.map((s) => <option key={s || 'empty'} value={s}>{s || 'none'}</option>)}
                  </select>
                </label>
                <label className="rfield rfield--wide">
                  <span>Summary</span>
                  <textarea className="wb-textarea" value={form.summary} onChange={set('summary')} rows={4} />
                </label>
                <label className="rfield rfield--wide">
                  <span>Advanced data (JSON)</span>
                  <textarea className="wb-textarea" style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)' }} value={form.data} onChange={set('data')} rows={8} spellCheck="false" />
                </label>
              </div>
              {jsonError && <div className="doc-error" style={{ marginTop: 10 }}><TriangleAlert size={14} strokeWidth={1.8} /> {jsonError}</div>}

              <div className="review-subgrid">
                <section>
                  <div className="dlabel">Source evidence</div>
                  {detail.sources.length === 0 && <div className="md-empty">No source chunks linked.</div>}
                  {detail.sources.map((s) => (
                    <div key={`${s.document_id}-${s.chunk_id}`} className="evidence-card">
                      <div className="evidence-head">
                        <span className={`src-badge src-${s.source}`}><i className="src-dot" aria-hidden="true" />{s.source}</span>
                        <span className="evidence-meta tnum">{s.title} · {s.author || 'unknown'}{s.doc_created_at ? ` · ${s.doc_created_at.slice(0, 10)}` : ''} · chunk {s.chunk_index}</span>
                      </div>
                      <p className="evidence-snip">{s.snippet}</p>
                    </div>
                  ))}
                </section>
                <section>
                  <div className="dlabel">Graph neighbors</div>
                  {detail.neighbors.length === 0 && <div className="md-empty">No graph links.</div>}
                  {detail.neighbors.map((n) => (
                    <div key={`${n.node_id}-${n.relation}-${n.direction}`} className="neighbor-row">
                      <Badge tone={KIND_TONE[n.kind] || 'neutral'} variant="outline" mono>{n.kind}</Badge>
                      <b>{n.label}</b>
                      <span className="neighbor-rel tnum">{n.direction === 'out' ? n.relation : `${n.relation} by`}{n.archived ? ' · archived' : ''}</span>
                    </div>
                  ))}
                </section>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
