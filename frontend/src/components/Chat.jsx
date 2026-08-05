import React, { useEffect, useState } from 'react'
import { Send } from 'lucide-react'
import { streamQuery } from '../api.js'
import Md from '../md.jsx'

export default function Chat({ pendingAsk, onOpenDoc }) {
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (pendingAsk?.question) setQuestion(pendingAsk.question)
  }, [pendingAsk])

  const ask = async (event) => {
    event?.preventDefault()
    if (!question.trim() || busy) return
    setBusy(true)
    setAnswer({ text: '', citations: [] })
    try {
      await streamQuery(question.trim(), {
        delta: ({ text = '' }) => setAnswer((current) => ({ ...current, text: current.text + text })),
        metadata: (metadata) => setAnswer((current) => ({ ...current, citations: metadata.citations || [] })),
        error: ({ message }) => setAnswer({ text: message || 'Unable to answer that question.', citations: [] }),
      })
    } catch (error) {
      setAnswer({ text: error.message, citations: [] })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="chat-col" style={{ padding: 'var(--sp-5)', height: '100%', overflow: 'auto' }}>
      <h1>Ask YBase</h1>
      <p className="settings-sub">Answers cite your connected Slack, GitHub, and Notion sources.</p>
      <form onSubmit={ask} className="settings-form">
        <input className="wb-input" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="What changed in the last decision?" autoFocus />
        <button className="wb-btn wb-btn--primary" disabled={busy || !question.trim()}><Send size={15} /> {busy ? 'Thinking…' : 'Ask'}</button>
      </form>
      {answer && <section className="chat-msg" style={{ marginTop: 'var(--sp-5)' }}><Md>{answer.text || 'Thinking…'}</Md>
        {answer.citations.map((citation) => <button key={citation.chunk_id} className="src-badge" onClick={() => onOpenDoc?.(citation.document_id, citation.quote || citation.snippet)}>{citation.title || citation.source}</button>)}
      </section>}
    </div>
  )
}
