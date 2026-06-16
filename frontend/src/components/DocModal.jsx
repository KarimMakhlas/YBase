import React, { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { getDocument } from '../api.js'
import { useToast } from './Toast.jsx'
import { SrcBadge } from '../whybase/ui.jsx'

// Full source document in an overlay — provenance click-through from
// citations, decisions, people, and search. Optionally highlights the cited
// chunk's text inside the document.
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
    if (highlight && text.includes(highlight)) {
      const i = text.indexOf(highlight)
      body = (
        <>
          {text.slice(0, i)}
          <mark className="doc-highlight" ref={(el) => el && el.scrollIntoView({ block: 'center' })}>
            {highlight}
          </mark>
          {text.slice(i + highlight.length)}
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
