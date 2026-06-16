import React, { useEffect, useState } from 'react'
import { getHealthDetails } from '../api.js'

// Admin-visible system strip: which models are live, what the formation
// queue is doing, when memory last changed. Catches a jammed pipeline at a
// glance instead of after four silent hours.
export default function StatusFooter() {
  const [health, setHealth] = useState(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    let timer
    const load = () =>
      getHealthDetails()
        .then((h) => { setHealth(h); setErr(false) })
        .catch(() => setErr(true))
        .finally(() => { timer = setTimeout(load, 30000) })
    load()
    return () => clearTimeout(timer)
  }, [])

  if (err) {
    return <footer className="statusbar bad">backend unreachable</footer>
  }
  if (!health) return null
  const f = health.formation || {}
  const busy = (f.pending || 0) + (f.processing || 0)
  const lastWrite = f.last_memory_write
    ? new Date(f.last_memory_write).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—'
  return (
    <footer className="statusbar">
      <span><i className={`dot ${health.db ? 'ok' : 'bad'}`} /> {health.llm_provider}: {health.llm_model}</span>
      <span>embeddings: {health.embeddings}</span>
      <span className={busy > 0 ? 'busy' : ''}>
        {busy > 0
          ? `forming: ${f.processing} active, ${f.pending} queued`
          : `queue idle${f.failed ? ` · ${f.failed} failed` : ''}`}
      </span>
      <span>last memory write: {lastWrite}</span>
      {health.slack_events && <span>slack: live</span>}
    </footer>
  )
}
