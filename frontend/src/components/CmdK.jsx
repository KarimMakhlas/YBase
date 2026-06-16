import React, { useEffect, useRef, useState } from 'react'
import { searchMemory } from '../api.js'
import { Badge, StatusBadge } from '../whybase/ui.jsx'

const TYPE_BADGE = {
  decision: 'decision', question: 'question', entity: 'entity',
  topic: 'topic', document: 'doc',
}
const TYPE_TONE = { decision: 'accent', question: 'warning', entity: 'info', topic: 'success', document: 'neutral' }

// Cmd-K palette: search everything in memory, jump to it in the right view.
export default function CmdK({ open, onClose, onPick }) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [sel, setSel] = useState(0)
  const inputRef = useRef(null)
  const timer = useRef(null)

  useEffect(() => {
    if (open) {
      setQ('')
      setResults([])
      setSel(0)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    clearTimeout(timer.current)
    if (!q.trim()) { setResults([]); return undefined }
    timer.current = setTimeout(() => {
      searchMemory(q.trim()).then((r) => { setResults(r); setSel(0) }).catch(() => {})
    }, 150)
    return () => clearTimeout(timer.current)
  }, [q, open])

  if (!open) return null

  const onKey = (e) => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, results.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
    else if (e.key === 'Enter' && results[sel]) { onPick(results[sel]); onClose() }
  }

  return (
    <div className="modal-backdrop cmdk-backdrop" onClick={onClose}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search decisions, people, questions, documents…"
          aria-label="Search memory"
        />
        <div className="cmdk-results">
          {q.trim() && results.length === 0 && (
            <div className="cmdk-empty">No matches in memory.</div>
          )}
          {results.map((r, i) => (
            <button
              key={`${r.type}-${r.id}`}
              className={`cmdk-item ${i === sel ? 'active' : ''}`}
              onMouseEnter={() => setSel(i)}
              onClick={() => { onPick(r); onClose() }}
            >
              <Badge tone={TYPE_TONE[r.type] || 'neutral'} variant="soft" mono>{TYPE_BADGE[r.type] || r.type}</Badge>
              <span className="cmdk-title">{r.title}</span>
              {r.status && <StatusBadge status={r.status} />}
              {r.detail && <span className="cmdk-detail">{r.detail}</span>}
            </button>
          ))}
        </div>
        <div className="cmdk-hint">↑↓ navigate · ↵ open · esc close</div>
      </div>
    </div>
  )
}
