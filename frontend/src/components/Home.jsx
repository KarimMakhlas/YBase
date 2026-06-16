import React, { useEffect, useState } from 'react'
import {
  Sparkles, RotateCcw, Calendar, RefreshCw, GitCommitHorizontal, ShieldCheck,
} from 'lucide-react'
import { getStats } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge } from '../whybase/ui.jsx'
import whybaseMark from '../assets/whybase-mark.svg'

const LAST_VISIT_KEY = 'sb:lastVisit'
const ONBOARD_DISMISS_KEY = 'sb:onboardingDismissed'

const SOURCE_LABEL = { slack: 'Slack', notion: 'Notion', github: 'GitHub', jira: 'Jira', meeting: 'Meeting' }
const fmtDate = (d) => (d ? d : '')

export default function Home({ onAsk, onNavigate, canAdmin = false, workspace, user }) {
  const [stats, setStats] = useState(null)
  const [question, setQuestion] = useState('')
  const [onboardDismissed, setOnboardDismissed] = useState(
    () => localStorage.getItem(ONBOARD_DISMISS_KEY) === '1',
  )
  const toast = useToast()

  const load = () => {
    const lastVisit = localStorage.getItem(LAST_VISIT_KEY)
    return getStats(lastVisit)
      .then((s) => {
        setStats(s)
        localStorage.setItem(LAST_VISIT_KEY, new Date().toISOString())
      })
      .catch((e) => toast(`Failed to load overview: ${e.message}`))
  }

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const docCount = stats?.counts?.documents ?? 0
  const showOnboarding = stats && !onboardDismissed && docCount <= 5
  const dismissOnboarding = () => {
    localStorage.setItem(ONBOARD_DISMISS_KEY, '1')
    setOnboardDismissed(true)
  }

  const submit = (e) => {
    e.preventDefault()
    if (question.trim()) onAsk(question.trim())
  }

  const c = stats?.counts
  const wsMetrics = c
    ? [
        { num: c.documents, lab: 'Documents', go: 'timeline' },
        { num: c.decisions, lab: 'Decisions', go: 'decisions' },
        { num: c.entities, lab: 'People & systems', go: 'people' },
        { num: c.open_questions, lab: 'Open questions', go: 'timeline' },
      ]
    : []

  const cards = c
    ? [
        { label: 'Documents ingested', value: c.documents, go: 'timeline' },
        { label: 'Decisions tracked', value: c.decisions, go: 'decisions' },
        { label: 'Open questions', value: c.open_questions, go: 'timeline' },
        { label: 'People & systems', value: c.entities, go: 'people' },
      ]
    : null

  const sources = stats?.sources || []
  const revisit = stats?.revisits?.[0]
  const role = workspace?.role || 'member'
  const wsName = workspace?.name || 'Your workspace'
  const decisionDates = (stats?.recent_decisions || []).map((d) => d.date).filter(Boolean).sort()
  const coverageStart = decisionDates[0]

  return (
    <div className="app-page home-page wb-reveal">
      <div className="eyebrow">Workspace · {wsName}</div>
      <h1 className="page-h1">Your team&apos;s memory, on demand</h1>
      <p className="page-lede">
        Decisions, reasoning, and history across Slack, Notion, GitHub and Jira — remembered,
        connected, and cited back to the message or doc that settled it. So you can answer the
        question that matters most: <em>why</em>.
      </p>

      <form className="ask-bar" onSubmit={submit}>
        <div className="wb-input-wrap">
          <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true">
            <Sparkles size={16} strokeWidth={1.8} />
          </span>
          <input
            className="wb-input wb-input--has-prefix"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder='Ask anything — e.g. "why did we choose Postgres?"'
            aria-label="Ask your team memory"
          />
        </div>
        <button className="wb-btn wb-btn--primary wb-btn--lg" type="submit">Ask</button>
      </form>

      {/* ---- Workspace hero frame ---- */}
      <section className="ws-frame wb-reveal" style={{ '--i': 1 }}>
        <div className="ws-frame-head">
          <div className="ws-id">
            <span className="ws-logo"><img src={whybaseMark} alt="" /></span>
            <div>
              <div className="eyebrow">Workspace</div>
              <h2>{wsName}</h2>
            </div>
          </div>
          <div className="ws-head-right">
            <Badge tone="success" variant="soft" mono dot>memory healthy</Badge>
            <Badge tone="accent" variant="soft" mono>{role}</Badge>
          </div>
        </div>

        <p className="ws-charter">
          Every decision {wsName} has made — <em>remembered</em> with its reasoning, linked across
          sources and time, and cited back to the message or doc that settled it.
        </p>

        <div className="ws-metrics">
          {wsMetrics.map((m) => (
            <button key={m.lab} className="ws-metric" onClick={() => m.go && onNavigate(m.go)} disabled={!m.go}>
              <span className="ws-metric-num tnum">{m.num}</span>
              <span className="ws-metric-lab">{m.lab}</span>
            </button>
          ))}
          {!c && [0, 1, 2, 3].map((i) => <div key={i} className="ws-metric wb-skeleton" style={{ height: 64 }} />)}
        </div>

        <div className="ws-frame-grid">
          <div className="ws-block">
            <div className="dlabel">Connected sources</div>
            <div className="ws-sources">
              {sources.length === 0 && <span className="md-empty">No sources connected yet.</span>}
              {sources.map((s) => (
                <button key={s.source} className="ws-src" onClick={() => onNavigate(canAdmin ? 'sources' : 'timeline')}>
                  <SrcBadge provider={s.source}>{SOURCE_LABEL[s.source] || s.source}</SrcBadge>
                  <Badge tone="neutral" variant="soft" mono>{s.n}</Badge>
                </button>
              ))}
            </div>
          </div>

          <div className="ws-block">
            <div className="dlabel">Coverage</div>
            <div className="ws-coverage">
              <div className="ws-cov-row">
                <Calendar size={15} strokeWidth={1.8} />
                <span className="tnum">{coverageStart ? `${coverageStart} — today` : 'building memory…'}</span>
              </div>
              <div className="ws-cov-row">
                <ShieldCheck size={15} strokeWidth={1.8} />
                <span>{c ? `${c.decisions} decisions · ${c.documents} documents` : '—'}</span>
              </div>
            </div>
          </div>

          <div className="ws-block">
            <div className="dlabel">Your access</div>
            <div className="ws-coverage">
              <div className="ws-cov-row">
                <RefreshCw size={15} strokeWidth={1.8} />
                <span>{user?.display_name || 'You'} · {role}</span>
              </div>
              <button className="ws-more" onClick={() => onNavigate(canAdmin ? 'settings' : 'people')}>
                {canAdmin ? 'Manage team →' : 'See people & systems →'}
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* ---- Relitigation banner ---- */}
      {revisit && (
        <div className="relit wb-reveal" style={{ '--i': 2 }} role="status">
          <RotateCcw size={17} strokeWidth={1.8} />
          <div>
            <b>Decision being relitigated.</b>{' '}
            <a onClick={() => onNavigate('decisions', { decisionId: revisit.old_id })} style={{ cursor: 'pointer' }}>
              “{revisit.old_title}”
            </a>{' '}
            is revisited by “{revisit.new_title}” ({revisit.when}).
          </div>
        </div>
      )}

      {/* ---- Onboarding (fresh workspace) ---- */}
      {showOnboarding && (
        <div className="relit wb-reveal" style={{ '--i': 3, alignItems: 'flex-start' }} role="status">
          <Sparkles size={17} strokeWidth={1.8} />
          <div style={{ flex: 1 }}>
            <b>Getting started.</b>{' '}
            {docCount > 0 ? 'Demo memory is loaded — try a question above, or ' : 'Loading demo memory — '}
            <a onClick={() => onAsk('Why did we choose Postgres over MongoDB?')} style={{ cursor: 'pointer' }}>
              ask your first question
            </a>
            {canAdmin && (
              <>
                {', '}
                <a onClick={() => onNavigate('sources')} style={{ cursor: 'pointer' }}>connect Slack</a>
                {' or '}
                <a onClick={() => onNavigate('add')} style={{ cursor: 'pointer' }}>add your own documents</a>
              </>
            )}.
            <button className="ws-more" style={{ marginLeft: 10 }} onClick={dismissOnboarding}>Dismiss</button>
          </div>
        </div>
      )}

      {/* ---- Stats ---- */}
      <div className="section-label">At a glance</div>
      <div className="stat-grid">
        {cards
          ? cards.map((card, i) => (
              <button key={card.label} className="stat-card wb-reveal" style={{ '--i': i + 4 }} onClick={() => onNavigate(card.go)}>
                <span className="num tnum">{card.value}</span>
                <span className="lab">{card.label}</span>
              </button>
            ))
          : [0, 1, 2, 3].map((i) => <div key={i} className="stat-card wb-skeleton" style={{ height: 92 }} />)}
      </div>

      {/* ---- Two-column lists ---- */}
      <div className="home-cols">
        <section className="list-card wb-reveal" style={{ '--i': 8 }}>
          <h3>Recent decisions</h3>
          {!stats && [0, 1, 2].map((i) => <div key={i} className="list-row"><div className="wb-skeleton" style={{ height: 14, flex: 1 }} /></div>)}
          {stats && stats.recent_decisions.length === 0 && (
            <div className="list-row"><span className="title" style={{ color: 'var(--text-tertiary)' }}>No decisions yet — add documents and memory formation will extract them.</span></div>
          )}
          {stats &&
            stats.recent_decisions.map((d) => (
              <div className="list-row" key={d.id}>
                <StatusBadge status={d.status} />
                <span className="title link" onClick={() => onNavigate('decisions', { decisionId: d.id })}>{d.title}</span>
                <span className="date tnum">{fmtDate(d.date)}</span>
              </div>
            ))}
          {stats && stats.recent_decisions.length > 0 && (
            <button className="list-more" onClick={() => onNavigate('decisions')}>View all decisions →</button>
          )}
        </section>

        <section className="list-card wb-reveal" style={{ '--i': 9 }}>
          <h3>Open questions</h3>
          {!stats && [0, 1, 2].map((i) => <div key={i} className="list-row"><div className="wb-skeleton" style={{ height: 14, flex: 1 }} /></div>)}
          {stats && stats.open_questions.length === 0 && stats.stale_questions?.length === 0 && (
            <div className="list-row"><span className="title" style={{ color: 'var(--text-tertiary)' }}>No open questions right now.</span></div>
          )}
          {stats &&
            stats.open_questions.map((q) => (
              <div className="list-row" key={q.id}>
                <GitCommitHorizontal size={15} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
                <span className="title">{q.title}</span>
                {q.date && <span className="date tnum">{q.date}</span>}
              </div>
            ))}
          {stats && stats.stale_questions?.map((q) => (
            <div className="list-row" key={`s-${q.id}`}>
              <Badge tone="warning" variant="soft" mono>{q.age_days}d</Badge>
              <span className="title">{q.title}</span>
            </div>
          ))}
          {stats && (stats.open_questions.length > 0 || stats.stale_questions?.length > 0) && (
            <button className="list-more" onClick={() => onAsk('What open questions do we have about scaling?')}>Ask about these →</button>
          )}
        </section>
      </div>
    </div>
  )
}
