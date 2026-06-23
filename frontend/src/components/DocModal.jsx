import React, { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { getDocument } from '../api.js'
import { useToast } from './Toast.jsx'
import { SrcBadge } from '../ybase/ui.jsx'

// Find the span of `highlight` within `text`, returning [start, end] or null.
// Tries an exact match first, then a whitespace-tolerant match so a near-verbatim
// citation quote (re-flowed spacing/newlines) still lands on the original text.
function findHighlight(text, highlight) {
  if (!highlight) return null
  const exact = text.indexOf(highlight)
  if (exact !== -1) return [exact, exact + highlight.length]
  const tokens = highlight.trim().split(/\s+/).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  if (!tokens.length) return null
  try {
    const m = new RegExp(tokens.join('\\s+')).exec(text)
    return m ? [m.index, m.index + m[0].length] : null
  } catch {
    return null
  }
}

// Full source document in an overlay — provenance click-through from
// citations, decisions, people, and search. Optionally highlights the cited
// quote (or chunk) inside the document.
export default function DocModal({ docId, highlight, onClose }) {
  const [doc, setDoc] = useState(null)
  const toast = useToast()

  useEffect(() => {
    setDoc(null)
    if (docId == null) return
    getDocument(docId, true)
      .then(setDoc)
      .catch((e) => { toast(`Failed to load document: ${e.message}`); onClose() })
  }, [docId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (docId == null) return null

  let body = null
  if (doc) {
    const text = doc.text || doc.text_preview || ''
    const span = findHighlight(text, highlight)
    if (span) {
      body = (
        <>
          {text.slice(0, span[0])}
          <mark className="doc-highlight" ref={(el) => el && el.scrollIntoView({ block: 'center' })}>
            {text.slice(span[0], span[1])}
          </mark>
          {text.slice(span[1])}
        </>
      )
    } else {
      body = text
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
      <div className="modal doc-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          {doc ? (
            <>
              <SrcBadge provider={doc.source}>{doc.source}</SrcBadge>
              <div className="modal-title">
                <h3>{doc.title}</h3>
                <span className="modal-meta tnum">
                  {doc.author || 'unknown author'}
                  {doc.doc_created_at ? ` · ${String(doc.doc_created_at).slice(0, 10)}` : ''}
                </span>
              </div>
            </>
          ) : (
            <div className="modal-title"><h3>Loading…</h3></div>
          )}
          <button className="wb-iconbtn wb-iconbtn--sm" onClick={onClose} aria-label="Close"><X size={16} strokeWidth={1.8} /></button>
        </div>
        <div className="modal-body">
          {!doc && <div className="skeleton skel-row" />}
          {doc && <pre className="doc-full">{body}</pre>}
        </div>
      </div>
    </div>
  )
}
