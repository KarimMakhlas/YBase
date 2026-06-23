import React, { useEffect, useState } from 'react'
import { Bell } from 'lucide-react'
import { getLatestDigest, listDigests, runDigest } from '../api.js'
import { useToast } from './Toast.jsx'
import { StatusBadge } from '../ybase/ui.jsx'

const SEEN_KEY = 'sb:digestSeen'

function DigestCard({ digest }) {
  const p = digest.payload
  const range = `${String(digest.period_start).slice(0, 10)} → ${String(digest.period_end).slice(0, 10)}`
  return (
    <div className="notif-card">
      <div className="notif-card-date">{range}</div>
      {p.empty && <div className="notif-empty">A quiet week — nothing new.</div>}
      {p.new_documents > 0 && (
        <div className="notif-line">{p.new_documents} new document{p.new_documents > 1 ? 's' : ''} remembered</div>
      )}
      {p.new_decisions.length > 0 && (
        <div className="notif-group">
          <span className="notif-label">New decisions</span>
          {p.new_decisions.map((d) => (
            <div key={d.id} className="notif-item">
              <StatusBadge status={d.status} /> {d.title}
            </div>
          ))}
        </div>
      )}
      {p.resolved_questions.length > 0 && (
        <div className="notif-group">
          <span className="notif-label">Questions resolved</span>
          {p.resolved_questions.map((q) => <div key={q.id} className="notif-item">{q.title}</div>)}
        </div>
      )}
      {p.opened_questions.length > 0 && (
        <div className="notif-group">
          <span className="notif-label">New questions</span>
          {p.opened_questions.map((q) => <div key={q.id} className="notif-item">{q.title}</div>)}
        </div>
      )}
      {p.stale_questions.length > 0 && (
        <div className="notif-group">
          <span className="notif-label">Still unanswered</span>
          {p.stale_questions.map((q) => (
            <div key={q.id} className="notif-item">{q.title} <em>({q.age_days}d)</em></div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Notifications({ isAdmin }) {
  const [open, setOpen] = useState(false)
  const [digests, setDigests] = useState(null)
  const [latestId, setLatestId] = useState(0)
  const [seenId, setSeenId] = useState(() => Number(localStorage.getItem(SEEN_KEY) || 0))
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  useEffect(() => {
    getLatestDigest().then((d) => { if (d && d.id) setLatestId(d.id) }).catch(() => {})
  }, [])

  const markSeen = (id) => {
    localStorage.setItem(SEEN_KEY, String(id))
    setSeenId(id)
  }

  const loadList = async () => {
    const list = await listDigests()
    setDigests(list)
    if (list[0]) { setLatestId(list[0].id); markSeen(list[0].id) }
  }

  const openPanel = async () => {
    setOpen(true)
    if (digests === null) {
      try { await loadList() } catch (e) { toast(`Failed to load digests: ${e.message}`) }
    } else if (digests[0]) {
      markSeen(digests[0].id)
    }
  }

  const generate = async () => {
    if (busy) return
    setBusy(true)
    try {
      await runDigest()
      await loadList()
      toast('Digest generated', 'success')
    } catch (e) {
      toast(`Generate failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const unread = latestId > seenId

  return (
    <div className="notif">
      <button
        className="wb-iconbtn notif-bell"
        onClick={() => (open ? setOpen(false) : openPanel())}
        title="Digests"
        aria-label="Digests"
      >
        <Bell size={17} strokeWidth={1.8} />
        {unread && <span className="notif-dot" />}
      </button>
      {open && (
        <>
          <div className="notif-backdrop" onClick={() => setOpen(false)} />
          <div className="notif-panel">
            <div className="notif-head">
              <strong>Digests</strong>
              {isAdmin && (
                <button className="linkbtn" onClick={generate} disabled={busy}>
                  {busy ? 'Generating…' : 'Generate now'}
                </button>
              )}
            </div>
            {digests === null && <div className="skeleton skel-row" />}
            {digests && digests.length === 0 && (
              <div className="home-empty">
                No digests yet.{isAdmin ? ' Generate one to preview.' : ' They arrive weekly.'}
              </div>
            )}
            {digests && digests.map((d) => <DigestCard key={d.id} digest={d} />)}
          </div>
        </>
      )}
    </div>
  )
}
