import React, { useEffect, useRef, useState } from 'react'
import { Upload, Sparkles, ChevronRight, TriangleAlert } from 'lucide-react'
import { deleteJSON, getJSON, postJSON } from '../api.js'
import { useToast } from './Toast.jsx'
import { SrcBadge, StatusBadge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

const SOURCES = ['slack', 'notion', 'github', 'jira', 'meeting', 'other']
const EMPTY_FORM = { source: 'slack', title: '', author: '', date: '', tags: '', text: '' }

function fmtCounts(counts) {
  const parts = []
  if (counts.decision) parts.push(`${counts.decision} decision${counts.decision > 1 ? 's' : ''}`)
  if (counts.question) parts.push(`${counts.question} question${counts.question > 1 ? 's' : ''}`)
  if (counts.entity) parts.push(`${counts.entity} entit${counts.entity > 1 ? 'ies' : 'y'}`)
  if (counts.topic) parts.push(`${counts.topic} topic${counts.topic > 1 ? 's' : ''}`)
  return parts.length ? parts.join(', ') : 'no new memory'
}

export default function AddMemory() {
  const [form, setForm] = useState(EMPTY_FORM)
  const [docs, setDocs] = useState(null)
  const [counts, setCounts] = useState({})
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)
  const [expanded, setExpanded] = useState(null)
  const fileRef = useRef(null)
  const watchingRef = useRef(new Set())
  const toast = useToast()

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  const load = () =>
    getJSON('/api/documents')
      .then((list) => setDocs([...list].sort((a, b) => b.id - a.id)))
      .catch((e) => toast(`Failed to load documents: ${e.message}`))

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!docs) return undefined
    const active = docs.some((d) => d.formation_status === 'pending' || d.formation_status === 'processing')
    if (!active) return undefined
    const t = setTimeout(load, 2500)
    return () => clearTimeout(t)
  }, [docs]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!docs) return
    for (const d of docs) {
      if (!watchingRef.current.has(d.id)) continue
      if (d.formation_status === 'complete') {
        watchingRef.current.delete(d.id)
        getJSON(`/api/documents/${d.id}`)
          .then((detail) => {
            setCounts((c) => ({ ...c, [d.id]: detail.memory_counts }))
            toast(`Memory formed from “${d.title}” — ${fmtCounts(detail.memory_counts)}`, 'success')
          })
          .catch(() => {})
      } else if (d.formation_status === 'failed') {
        watchingRef.current.delete(d.id)
        toast(`Memory formation failed for “${d.title}”`)
      }
    }
  }, [docs, toast])

  const readFile = (file) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      setForm((f) => ({
        ...f,
        text: String(reader.result),
        title: f.title || file.name.replace(/\.(md|markdown|txt)$/i, ''),
      }))
    }
    reader.onerror = () => toast(`Could not read ${file.name}`)
    reader.readAsText(file)
  }

  const onDrop = (e) => { e.preventDefault(); setDrag(false); readFile(e.dataTransfer.files?.[0]) }

  const retry = async (id) => {
    try {
      await postJSON(`/api/documents/${id}/reform`, {})
      watchingRef.current.add(id)
      await load()
    } catch (e) {
      toast(`Retry failed: ${e.message}`)
    }
  }

  const remove = async (id, title) => {
    if (!window.confirm(`Delete “${title}” and the memory only it supports?`)) return
    try {
      const res = await deleteJSON(`/api/documents/${id}`)
      setExpanded(null)
      toast(
        res.orphaned_memory_removed
          ? `Deleted — ${res.orphaned_memory_removed} memory node${res.orphaned_memory_removed > 1 ? 's' : ''} removed with it`
          : 'Document deleted',
        'success'
      )
      await load()
    } catch (e) {
      toast(`Delete failed: ${e.message}`)
    }
  }

  const toggleExpand = async (id) => {
    if (expanded?.id === id) { setExpanded(null); return }
    try {
      setExpanded({ id, loading: true })
      const detail = await getJSON(`/api/documents/${id}`)
      setExpanded({ id, detail })
    } catch (e) {
      setExpanded(null)
      toast(`Failed to load document: ${e.message}`)
    }
  }

  const submit = async (e) => {
    e.preventDefault()
    if (busy || !form.title.trim() || !form.text.trim()) return
    setBusy(true)
    try {
      const res = await postJSON('/api/ingest', {
        source: form.source,
        title: form.title.trim(),
        text: form.text,
        author: form.author.trim() || null,
        created_at: form.date || null,
        tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
      })
      watchingRef.current.add(res.document_id)
      setForm((f) => ({ ...EMPTY_FORM, source: f.source, author: f.author }))
      await load()
    } catch (err) {
      toast(`Ingest failed: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app-page wb-reveal">
      <PageHeader
        kicker="Add to memory"
        title={<>Feed the <em>memory</em>.</>}
        lede="Drop in a Slack thread, meeting notes, a spec or a ticket — YBase extracts the decisions, people and open questions."
      />

      <form className="add-form wb-reveal" style={{ '--i': 1 }} onSubmit={submit}>
        <div
          className={`dropzone ${drag ? 'drag' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => fileRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === 'Enter') fileRef.current?.click() }}
        >
          <Upload size={22} strokeWidth={1.8} />
          <span>Drop a <b>.md</b> / <b>.txt</b> file here, or click to browse</span>
          <small>…or just paste the content below</small>
          <input ref={fileRef} type="file" accept=".md,.markdown,.txt,text/*" hidden onChange={(e) => readFile(e.target.files?.[0])} />
        </div>

        <textarea className="wb-textarea add-text" value={form.text} onChange={set('text')} placeholder="Paste the document content here…" rows={9} aria-label="Document content" />

        <div className="add-grid">
          <label className="rfield">
            <span>Source</span>
            <select className="wb-select" value={form.source} onChange={set('source')}>
              {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="rfield">
            <span>Title <i className="req">*</i></span>
            <input className="wb-input" value={form.title} onChange={set('title')} placeholder="e.g. #eng-db thread: Postgres vs MongoDB" />
          </label>
          <label className="rfield">
            <span>Author</span>
            <input className="wb-input" value={form.author} onChange={set('author')} placeholder="e.g. Maya Chen" />
          </label>
          <label className="rfield">
            <span>Original date</span>
            <input className="wb-input" type="date" value={form.date} onChange={set('date')} />
          </label>
          <label className="rfield rfield--wide">
            <span>Tags</span>
            <input className="wb-input" value={form.tags} onChange={set('tags')} placeholder="comma-separated, e.g. database, scaling" />
          </label>
        </div>

        <div className="add-actions">
          <button className="wb-btn wb-btn--primary" type="submit" disabled={busy || !form.title.trim() || !form.text.trim()}>
            <Sparkles size={15} strokeWidth={1.8} /> {busy ? 'Adding…' : 'Add to memory'}
          </button>
        </div>
      </form>

      <div className="section-label">In memory{docs ? ` · ${docs.length} documents` : ''}</div>
      <section className="doc-library">
        {!docs && [0, 1, 2].map((i) => <div key={i} className="wb-skeleton" style={{ height: 56, borderRadius: 'var(--radius-md)' }} />)}
        {docs && docs.length === 0 && <div className="md-empty">Nothing ingested yet — this will be the first document.</div>}
        {docs && docs.map((d) => (
          <div key={d.id} className={expanded?.id === d.id ? 'doc-row open' : 'doc-row'}>
            <button className="doc-row-head" onClick={() => toggleExpand(d.id)}>
              <SrcBadge provider={d.source}>{d.source}</SrcBadge>
              <div className="doc-main">
                <div className="doc-title">{d.title}</div>
                <div className="doc-meta tnum">
                  {d.author || 'unknown author'}
                  {d.doc_created_at ? ` · ${d.doc_created_at.slice(0, 10)}` : ''}
                  {counts[d.id] ? ` · extracted: ${fmtCounts(counts[d.id])}` : ''}
                </div>
              </div>
              <StatusBadge status={d.formation_status} dot>{d.formation_status === 'processing' ? 'forming' : d.formation_status}</StatusBadge>
              <ChevronRight size={17} strokeWidth={1.8} className="doc-chev" />
            </button>
            {expanded?.id === d.id && (
              <div className="doc-detail wb-reveal">
                {d.formation_status === 'failed' && d.formation_error && (
                  <div className="doc-error"><TriangleAlert size={14} strokeWidth={1.8} /> {d.formation_error.split('\n').slice(-2).join(' ').slice(0, 220)}</div>
                )}
                {expanded.loading && <div className="wb-skeleton" style={{ height: 40 }} />}
                {expanded.detail && (
                  <>
                    {expanded.detail.context_summary && <p className="doc-summary">{expanded.detail.context_summary}</p>}
                    {Object.keys(expanded.detail.memory_counts || {}).length > 0 && (
                      <p className="doc-summary" style={{ color: 'var(--text-tertiary)' }}>In memory: {fmtCounts(expanded.detail.memory_counts)}</p>
                    )}
                    <pre className="doc-preview">{expanded.detail.text_preview}</pre>
                  </>
                )}
                <div className="doc-detail-actions">
                  {d.formation_status === 'failed' && (
                    <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => retry(d.id)}>Retry formation</button>
                  )}
                  <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => remove(d.id, d.title)}>Delete</button>
                </div>
              </div>
            )}
          </div>
        ))}
      </section>
    </div>
  )
}
