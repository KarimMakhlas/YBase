import React, { useEffect, useRef, useState } from 'react'
import {
  Plus, MessageSquare, Trash2, PanelLeftClose, PanelLeftOpen, Copy, RefreshCw,
  ChevronRight, Scale, Sparkles, FileText, Hash, SquareCheck, Calendar, ThumbsUp,
} from 'lucide-react'
import {
  streamQuery, listSessions, createSession, getSession, saveMessage, deleteSession,
  submitAnswerFeedback, getMyAnswerFeedback,
} from '../api.js'
import Md from '../md.jsx'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge, Spinner } from '../whybase/ui.jsx'
import whybaseMark from '../assets/whybase-mark.svg'

const STARTERS = [
  'Why did we choose Postgres over MongoDB?',
  'Was the database decision ever revisited?',
  'What open questions do we have about scaling?',
  'Why do we use pgvector instead of a dedicated vector DB?',
]

const SOURCE_ICON = { slack: Hash, notion: FileText, github: SquareCheck, jira: SquareCheck, meeting: Calendar }
const CONF_TONE = { high: 'success', medium: 'warning', low: 'danger' }
const ISSUE_OPTIONS = [
  ['wrong', 'Wrong answer'],
  ['missing_citation', 'Missing citation'],
  ['bad_citation', 'Bad citation'],
  ['outdated', 'Outdated'],
  ['not_in_memory', 'Not in memory'],
  ['other', 'Other'],
]
const ISSUE_LABELS = Object.fromEntries([['helpful', 'Helpful'], ...ISSUE_OPTIONS])
const CARD_META = {
  why_it_won: { label: 'Why it won', Icon: Sparkles },
  tradeoffs: { label: 'Trade-offs', Icon: Scale },
  alternatives: { label: 'Alternatives', Icon: FileText },
  open_questions: { label: 'Open questions', Icon: MessageSquare },
  decision_anatomy: { label: 'Decision anatomy', Icon: SquareCheck },
}

function citationLabel(citation) {
  if (!citation) return ''
  const title = citation.title || `chunk ${citation.chunk_id}`
  return `${title} · C${citation.chunk_id}`
}

function Citations({ citations, onOpenDoc, onFlagCitation }) {
  const [open, setOpen] = useState(false)
  if (!citations || !citations.length) return null
  return (
    <>
      <button className={`sources-toggle ${open ? 'open' : ''}`} onClick={() => setOpen(!open)} aria-expanded={open}>
        <ChevronRight size={14} strokeWidth={2} className="chev" /> Sources ({citations.length})
      </button>
      {open && (
        <div className="citations">
          {citations.map((c) => (
            <div
              className="citation"
              key={c.chunk_id}
              id={`cite-${c.chunk_id}`}
              role="button"
              tabIndex={0}
              title="Open the full source document"
              onClick={() => onOpenDoc && onOpenDoc(c.document_id, c.text || c.snippet)}
              onKeyDown={(e) => e.key === 'Enter' && onOpenDoc && onOpenDoc(c.document_id, c.text || c.snippet)}
            >
              <div className="citation-head">
                <SrcBadge provider={c.source}>{c.source}</SrcBadge>
                <span className="citation-meta">
                  C{c.chunk_id} · {c.title} · {c.author || 'unknown'} · {c.date || 'undated'}
                </span>
                {onFlagCitation && (
                  <button
                    className="msg-action"
                    style={{ marginLeft: 'auto' }}
                    onClick={(e) => { e.stopPropagation(); onFlagCitation(c) }}
                  >
                    flag
                  </button>
                )}
              </div>
              <div className="citation-snip">{c.snippet}</div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

function Trace({ trace }) {
  const [open, setOpen] = useState(false)
  if (!trace || (!trace.nodes?.length && !trace.seed_chunks)) return null
  return (
    <>
      <button className={`sources-toggle ${open ? 'open' : ''}`} onClick={() => setOpen(!open)} aria-expanded={open}>
        <ChevronRight size={14} strokeWidth={2} className="chev" /> How I remembered this
      </button>
      {open && (
        <div className="mini-tl" style={{ borderLeftColor: 'var(--border-strong)' }}>
          <div className="ev">
            <span className="d">{trace.seed_chunks}</span> chunks matched by similarity
            {trace.graph_chunks > 0 && ` · ${trace.graph_chunks} more pulled in through ${trace.edges} graph edges`}
          </div>
          {trace.nodes?.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
              {trace.nodes.map((n) => (
                <span key={n.id} className={`wb-tag ${n.kind === 'decision' ? 'wb-tag--accent' : ''}`}>
                  {n.label}{n.status ? ` · ${n.status}` : ''}
                </span>
              ))}
            </div>
          )}
          {trace.entities?.length > 0 && (
            <div className="ev">people &amp; systems: {trace.entities.join(', ')}</div>
          )}
        </div>
      )}
    </>
  )
}

function NoMemory({ meta, onAsk, canAdd, onAddDoc }) {
  return (
    <div className="no-memory">
      <Scale size={16} strokeWidth={1.8} />
      <div>
        <b style={{ color: 'var(--text)' }}>Memory doesn&apos;t directly cover this.</b>
        {meta.trace?.nodes?.length > 0 && (
          <span> Nearest remembered context: {meta.trace.nodes.slice(0, 3).map((n) => n.label).join(' · ')}.</span>
        )}
        {meta.related_questions?.length > 0 && (
          <div className="related" style={{ marginTop: 10 }}>
            <div className="related-title">Memory could answer</div>
            {meta.related_questions.map((q) => (
              <button key={q} className="chip-q" onClick={() => onAsk(q)}>{q}</button>
            ))}
          </div>
        )}
        {canAdd && (
          <button className="list-more" style={{ paddingTop: 10 }} onClick={onAddDoc}>Add a document about this →</button>
        )}
      </div>
    </div>
  )
}

function InsightCards({ cards, onOpenDoc }) {
  if (!cards || cards.length === 0) return null
  return (
    <div className="insight-cards">
      {cards.map((card, idx) => {
        const meta = CARD_META[card.type] || CARD_META.decision_anatomy
        const Icon = meta.Icon
        return (
          <section className={`insight-card insight-card--${card.type || 'decision_anatomy'}`} key={`${card.type || 'card'}-${idx}`}>
            <div className="insight-card-head">
              <span className="insight-card-kicker"><Icon size={14} strokeWidth={1.8} /> {meta.label}</span>
              <h4>{card.title}</h4>
            </div>
            <div className="insight-card-items">
              {(card.items || []).map((item, itemIdx) => (
                <div className="insight-card-item" key={`${item.label || 'item'}-${itemIdx}`}>
                  {item.label && <b>{item.label}</b>}
                  {item.detail && <span>{item.detail}</span>}
                  {item.sources && item.sources.length > 0 && (
                    <div className="insight-card-srcs">
                      {item.sources.map((src, srcIdx) => (
                        <button
                          key={`${src.chunk_id}-${srcIdx}`}
                          className="src-badge"
                          onClick={() => onOpenDoc && onOpenDoc(src.document_id)}
                          title="Open the source"
                        >
                          <i className="src-dot" /> {src.source}: {src.title}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function AssistantExtras({ meta, onAsk, onOpenDoc, canAdd, onAddDoc, onCopy, onRegen, onFlagCitation }) {
  if (!meta) return null
  const empty = !meta.citations || meta.citations.length === 0
  return (
    <>
      <div className="meta-row">
        <Badge tone={CONF_TONE[meta.confidence] || 'neutral'} variant="outline" dot>confidence: {meta.confidence}</Badge>
        <span className="msg-actions">
          <button className="msg-action" onClick={onCopy} title="Copy answer"><Copy size={13} strokeWidth={1.8} /> Copy</button>
          <button className="msg-action" onClick={onRegen} title="Ask again"><RefreshCw size={13} strokeWidth={1.8} /> Regenerate</button>
        </span>
      </div>
      {empty && <NoMemory meta={meta} onAsk={onAsk} canAdd={canAdd} onAddDoc={onAddDoc} />}
      {meta.timeline && meta.timeline.length > 0 && (
        <div className="mini-tl">
          {meta.timeline.map((t, j) => (
            <div className="ev" key={j}><span className="d">{t.date}</span>{t.event}</div>
          ))}
        </div>
      )}
      {meta.counter_evidence && meta.counter_evidence.length > 0 && (
        <div className="counter-ev">
          <div className="counter-ev-title"><Scale size={14} strokeWidth={1.8} /> Pushback &amp; counter-evidence</div>
          {meta.counter_evidence.map((ce, j) => (
            <div className="counter-ev-item" key={j}>
              <div>{ce.point}</div>
              {ce.evidence && ce.evidence.length > 0 && (
                <div className="counter-ev-srcs">
                  {ce.evidence.map((e, k) => (
                    <button
                      key={k}
                      className="src-badge"
                      style={{ cursor: 'pointer' }}
                      onClick={() => onOpenDoc && onOpenDoc(e.document_id)}
                      title="Open the source"
                    >
                      <i className={`src-dot`} /> {e.source}: {e.title}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <InsightCards cards={meta.insight_cards} onOpenDoc={onOpenDoc} />
      <Citations citations={meta.citations} onOpenDoc={onOpenDoc} onFlagCitation={onFlagCitation} />
      <Trace trace={meta.trace} />
      {!empty && meta.related_questions && meta.related_questions.length > 0 && (
        <div className="related">
          <div className="related-title">Worth asking next</div>
          {meta.related_questions.map((q) => (
            <button key={q} className="chip-q" onClick={() => onAsk(q)}>{q}</button>
          ))}
        </div>
      )}
    </>
  )
}

function AnswerFeedbackControls({ message, refreshKey, onFlag }) {
  const [current, setCurrent] = useState(null)
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  useEffect(() => {
    let cancelled = false
    if (!message.id) return undefined
    getMyAnswerFeedback(message.id)
      .then((fb) => { if (!cancelled) setCurrent(fb) })
      .catch(() => { if (!cancelled) setCurrent(null) })
    return () => { cancelled = true }
  }, [message.id, refreshKey])

  const markHelpful = async () => {
    if (!message.id || busy) return
    setBusy(true)
    try {
      const fb = await submitAnswerFeedback({ chat_message_id: message.id, issue_type: 'helpful' })
      setCurrent(fb)
      toast('Feedback saved', 'success')
    } catch (e) {
      toast(`Feedback failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const flagged = current && current.issue_type !== 'helpful'
  return (
    <div className="meta-row" style={{ marginTop: 'var(--sp-3)' }}>
      <button className="msg-action" onClick={markHelpful} disabled={busy}><ThumbsUp size={13} strokeWidth={1.8} /> Helpful</button>
      <button className="msg-action" onClick={() => onFlag(null)} disabled={busy}>Flag issue</button>
      {current?.issue_type === 'helpful' && <Badge tone="success" variant="soft" mono>marked helpful</Badge>}
      {flagged && (
        <StatusBadge status={current.status}>{ISSUE_LABELS[current.issue_type] || current.issue_type} · {current.status}</StatusBadge>
      )}
    </div>
  )
}

function FlagIssueForm({ draft, citations, onCancel, onSubmitted }) {
  const [issueType, setIssueType] = useState('wrong')
  const [note, setNote] = useState('')
  const [citedChunkId, setCitedChunkId] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  useEffect(() => {
    setIssueType('wrong')
    setNote('')
    setCitedChunkId(draft?.citedChunkId ? String(draft.citedChunkId) : '')
  }, [draft?.messageId, draft?.citedChunkId])

  const submit = async (e) => {
    e.preventDefault()
    if (!draft?.messageId || busy) return
    if (!ISSUE_OPTIONS.some(([value]) => value === issueType)) { toast('Choose an issue type'); return }
    setBusy(true)
    try {
      const fb = await submitAnswerFeedback({
        chat_message_id: draft.messageId,
        issue_type: issueType,
        note,
        cited_chunk_id: citedChunkId ? Number(citedChunkId) : null,
      })
      toast('Issue flagged', 'success')
      onSubmitted(fb)
    } catch (err) {
      toast(`Flag failed: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="flag-form" onSubmit={submit} style={{ marginTop: 'var(--sp-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)', background: 'var(--surface-inset)', padding: 'var(--sp-4)' }}>
      <div className="review-grid">
        <label className="rfield">
          <span>Issue</span>
          <select className="wb-select" value={issueType} onChange={(e) => setIssueType(e.target.value)}>
            {ISSUE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="rfield">
          <span>Citation</span>
          <select className="wb-select" value={citedChunkId} onChange={(e) => setCitedChunkId(e.target.value)}>
            <option value="">No specific citation</option>
            {(citations || []).map((c) => <option key={c.chunk_id} value={c.chunk_id}>{citationLabel(c)}</option>)}
          </select>
        </label>
        <label className="rfield rfield--wide">
          <span>Note</span>
          <textarea className="wb-textarea" value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder="What should an admin check?" />
        </label>
      </div>
      <div className="review-actions" style={{ marginTop: 'var(--sp-3)' }}>
        <button type="button" className="wb-btn wb-btn--ghost wb-btn--sm" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" className="wb-btn wb-btn--primary wb-btn--sm" disabled={busy}>Submit flag</button>
      </div>
    </form>
  )
}

export default function Chat({ pendingAsk, canAdd, onAddDoc, onOpenDoc }) {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 900)
  const [feedbackDraft, setFeedbackDraft] = useState(null)
  const [feedbackRefresh, setFeedbackRefresh] = useState(0)
  const scrollRef = useRef(null)
  const textareaRef = useRef(null)
  const activeIdRef = useRef(null)
  const toast = useToast()
  activeIdRef.current = activeId

  useEffect(() => {
    listSessions().then(setSessions).catch((e) => toast(`Failed to load history: ${e.message}`))
  }, [toast])

  const lastAskRef = useRef(0)
  useEffect(() => {
    if (pendingAsk && pendingAsk.question && pendingAsk.n !== lastAskRef.current) {
      lastAskRef.current = pendingAsk.n
      ask(pendingAsk.question, { newSession: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingAsk])

  const scrollDown = () => {
    requestAnimationFrame(() => {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
    })
  }

  const ask = async (question, { newSession = false } = {}) => {
    if (!question.trim() || busy) return
    setBusy(true)
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    if (newSession) setMessages([])

    let sid = newSession ? null : activeIdRef.current
    if (!sid) {
      try {
        const sess = await createSession(question.slice(0, 80))
        sid = sess.id
        setActiveId(sid)
        setSessions((s) => [sess, ...s])
      } catch (e) {
        toast(`Could not save conversation: ${e.message}`)
      }
    }

    const history = (newSession ? [] : messages)
      .filter((m) => m.text)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.text }))

    setMessages((m) => [
      ...(newSession ? [] : m),
      { role: 'user', text: question },
      { role: 'assistant', text: '', status: 'Searching memory…', meta: null, streaming: true },
    ])
    scrollDown()

    if (sid) saveMessage(sid, 'user', question).catch(() => {})

    const update = (fn) =>
      setMessages((m) => {
        const next = m.slice()
        next[next.length - 1] = fn(next[next.length - 1])
        return next
      })

    let finalText = ''
    let finalMeta = null
    try {
      await streamQuery(question, {
        status: (s) => { update((msg) => ({ ...msg, status: s.message })); scrollDown() },
        delta: (d) => {
          finalText += d.text
          update((msg) => ({ ...msg, status: null, text: msg.text + d.text }))
          scrollDown()
        },
        metadata: (meta) => { finalMeta = meta; update((msg) => ({ ...msg, meta })); scrollDown() },
        error: (e) => {
          update((msg) => ({ ...msg, status: null, text: msg.text + `\n\n**Error:** ${e.message}` }))
          toast(e.message)
        },
        done: () => {},
      }, history)
    } catch (e) {
      update((msg) => ({ ...msg, status: null, text: msg.text + `\n\n**Error:** ${e.message}` }))
      toast(`Query failed: ${e.message}`)
    } finally {
      update((msg) => ({ ...msg, streaming: false }))
      setBusy(false)
      if (sid && finalText) {
        try {
          const saved = await saveMessage(sid, 'assistant', finalText, finalMeta)
          update((msg) => ({ ...msg, id: saved.id, persisted: true }))
        } catch (e) {
          update((msg) => ({ ...msg, persistError: true }))
          toast(`Could not save answer: ${e.message}`)
        }
        setSessions((s) => {
          const found = s.find((x) => x.id === sid)
          if (!found) return s
          return [found, ...s.filter((x) => x.id !== sid)]
        })
      }
    }
  }

  const openSession = async (id) => {
    if (busy || id === activeId) return
    try {
      const sess = await getSession(id)
      setActiveId(id)
      setMessages(
        sess.messages.map((m) => ({
          id: m.id, role: m.role, text: m.content, meta: m.meta, created_at: m.created_at,
        }))
      )
      setFeedbackDraft(null)
      scrollDown()
    } catch (e) {
      toast(`Failed to open conversation: ${e.message}`)
    }
  }

  const newChat = () => {
    if (busy) return
    setActiveId(null)
    setMessages([])
    setFeedbackDraft(null)
  }

  const removeSession = async (id, ev) => {
    ev.stopPropagation()
    try {
      await deleteSession(id)
      setSessions((s) => s.filter((x) => x.id !== id))
      if (id === activeId) newChat()
    } catch (e) {
      toast(`Delete failed: ${e.message}`)
    }
  }

  const onCite = (id) => {
    const el = document.getElementById(`cite-${id}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      el.classList.add('flash')
      setTimeout(() => el.classList.remove('flash'), 1200)
    }
  }

  const onInputChange = (e) => {
    setInput(e.target.value)
    const ta = e.target
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      ask(input)
    }
  }

  const openFlagForm = (message, citation = null) => {
    if (!message.id) return
    setFeedbackDraft({
      messageId: message.id,
      citedChunkId: citation?.chunk_id || '',
      citationLabel: citation ? citationLabel(citation) : '',
    })
  }

  return (
    <div className="chat-wrap">
      <aside className={`chat-sidebar ${sidebarOpen ? '' : 'closed'}`}>
        <div className="chat-sidebar-head">
          <button className="wb-btn wb-btn--secondary wb-btn--block" onClick={newChat}>
            <Plus size={15} strokeWidth={1.8} /> New conversation
          </button>
        </div>
        <div className="session-list">
          {sessions.length === 0 && <div className="session-empty">No conversations yet.</div>}
          {sessions.map((s) => (
            <div
              key={s.id}
              role="button"
              tabIndex={0}
              className={`session-item ${s.id === activeId ? 'active' : ''}`}
              onClick={() => openSession(s.id)}
              onKeyDown={(e) => e.key === 'Enter' && openSession(s.id)}
            >
              <MessageSquare size={15} strokeWidth={1.8} />
              <span className="session-title">{s.title}</span>
              <button className="session-del" title="Delete conversation" onClick={(e) => removeSession(s.id, e)}>
                <Trash2 size={14} strokeWidth={1.8} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <div className="chat-main">
        <div className="chat-toolbar">
          <button
            className="wb-iconbtn wb-iconbtn--sm"
            title={sidebarOpen ? 'Hide history' : 'Show history'}
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            {sidebarOpen ? <PanelLeftClose size={16} strokeWidth={1.8} /> : <PanelLeftOpen size={16} strokeWidth={1.8} />}
          </button>
        </div>
        <div className="chat-scroll" ref={scrollRef}>
          <div className="chat-col">
            {messages.length === 0 && (
              <div className="chat-empty wb-reveal">
                <h2>Ask your company&apos;s memory</h2>
                <p>
                  Answers come from decisions, reasoning and history across Slack, Notion, GitHub and
                  Jira — every claim cited back to its source.
                </p>
                <div className="starters">
                  {STARTERS.map((s) => (
                    <button key={s} className="chip-q" onClick={() => ask(s)}>{s}</button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) =>
              m.role === 'user' ? (
                <div className="msg user wb-reveal" key={i}>
                  <div className="msg-role">You</div>
                  <div className="msg-body">{m.text}</div>
                </div>
              ) : (
                <div className="msg assistant" key={i}>
                  <div className="msg-role">
                    <img src={whybaseMark} width="15" height="15" alt="" /> WhyBase
                  </div>
                  {m.status && <div className="thinking"><Spinner size="sm" /> {m.status}</div>}
                  <div className="msg-body">
                    <Md text={m.text} onCite={onCite} />
                    {m.streaming && !m.status && <span className="wb-caret" />}
                  </div>
                  <AssistantExtras
                    meta={m.meta}
                    onAsk={ask}
                    onOpenDoc={onOpenDoc}
                    canAdd={canAdd}
                    onAddDoc={onAddDoc}
                    onCopy={() => {
                      navigator.clipboard.writeText(m.text).then(
                        () => toast('Answer copied', 'success'),
                        () => toast('Copy failed')
                      )
                    }}
                    onRegen={() => {
                      const prev = messages[i - 1]
                      if (prev && prev.role === 'user') ask(prev.text)
                    }}
                    onFlagCitation={(citation) => openFlagForm(m, citation)}
                  />
                  {m.persistError && (
                    <div className="no-memory" style={{ borderColor: 'var(--danger-border)' }}>
                      <Scale size={16} strokeWidth={1.8} />
                      <div>Feedback unavailable because this answer was not saved.</div>
                    </div>
                  )}
                  {m.id && !m.streaming && m.text && (
                    <AnswerFeedbackControls
                      message={m}
                      refreshKey={feedbackRefresh}
                      onFlag={(citation) => openFlagForm(m, citation)}
                    />
                  )}
                  {feedbackDraft?.messageId === m.id && (
                    <FlagIssueForm
                      draft={feedbackDraft}
                      citations={m.meta?.citations || []}
                      onCancel={() => setFeedbackDraft(null)}
                      onSubmitted={() => {
                        setFeedbackDraft(null)
                        setFeedbackRefresh((n) => n + 1)
                      }}
                    />
                  )}
                </div>
              )
            )}
          </div>
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); ask(input) }}>
          <div className="composer-inner">
            <textarea
              ref={textareaRef}
              className="wb-textarea"
              rows={1}
              value={input}
              onChange={onInputChange}
              onKeyDown={onKeyDown}
              placeholder='Ask anything — e.g. "why do we use Postgres?"'
              disabled={busy}
            />
            <button className="wb-btn wb-btn--primary wb-btn--lg" type="submit" disabled={busy || !input.trim()}>
              {busy ? '…' : 'Ask'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
