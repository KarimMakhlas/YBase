import React, { useEffect, useState, lazy, Suspense } from 'react'
import {
  Sparkles, RotateCcw, Calendar, RefreshCw, GitCommitHorizontal, ShieldCheck,
  Plug, ArrowRight, ArrowUpRight,
} from 'lucide-react'
import { motion, MotionConfig } from 'framer-motion'
import { getStats } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge } from '../ybase/ui.jsx'
import { buildStarters } from '../lib/starters.js'
import { useThemeColors, SOURCE_VARS } from '../ybase/charts.js'
import { staggerContainer, fadeUp } from '../ybase/motionPresets.js'
import CountUp from '../ybase/CountUp.jsx'
import PageHeader from '../ybase/PageHeader.jsx'
import SetupChecklist from './SetupChecklist.jsx'
import '../ybase/home.css'

const LAST_VISIT_KEY = 'sb:lastVisit'
const SOURCE_LABEL = { slack: 'Slack', notion: 'Notion', github: 'GitHub', jira: 'Jira', meeting: 'Meeting' }

// Recharts is heavy — load the charts lazily so it's a separate chunk, fetched
// only on the home page and never by logged-out marketing visitors.
const SourceDonut = lazy(() => import('./HomeCharts.jsx').then((m) => ({ default: m.SourceDonut })))
const ResolutionGauge = lazy(() => import('./HomeCharts.jsx').then((m) => ({ default: m.ResolutionGauge })))
const ChartFallback = () => <div className="wb-skeleton" style={{ height: 172, borderRadius: 'var(--radius-md)' }} />

export default function Home({ onAsk, onNavigate, canAdmin = false, workspace, user, setup, onInvite }) {
  const [stats, setStats] = useState(null)
  const [question, setQuestion] = useState('')
  const toast = useToast()

  const srcColors = useThemeColors(SOURCE_VARS)
  const tc = useThemeColors({ accent: '--accent', track: '--border', muted: '--text-tertiary' })

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

  const submit = (e) => {
    e.preventDefault()
    if (question.trim()) onAsk(question.trim())
  }

  const c = stats?.counts
  const sources = stats?.sources || []
  const revisit = stats?.revisits?.[0]
  const role = workspace?.role || 'member'
  const decisionDates = (stats?.recent_decisions || []).map((d) => d.date).filter(Boolean).sort()
  const coverageStart = decisionDates[0]
  const starters = buildStarters(stats)

  const metrics = c
    ? [
        { num: c.documents, lab: 'Documents', go: 'timeline' },
        { num: c.decisions, lab: 'Decisions', go: 'decisions' },
        { num: c.entities, lab: 'People & systems', go: 'people' },
        { num: c.open_questions, lab: 'Open questions', go: 'timeline' },
      ]
    : []

  const totalDocs = sources.reduce((a, s) => a + (s.n || 0), 0) || (c?.documents ?? 0)
  const resolveTotal = c ? c.decisions + c.open_questions : 0
  const resolvePct = resolveTotal > 0 ? Math.round((c.decisions / resolveTotal) * 100) : 0

  return (
    <MotionConfig reducedMotion="user">
      <motion.div
        className="app-page wb-home"
        variants={staggerContainer(0.07)}
        initial="hidden"
        animate="show"
      >
        {/* ---- Branded hero: the ask is the product ---- */}
        <PageHeader
          align="center"
          kicker="Workspace memory"
          title={<>Never lose a <em>decision</em> again.</>}
          lede="Ask in plain English. YBase answers from your team's Slack, Notion, GitHub and Jira — every claim cited to the source that settled it."
        >
          <form className="home-ask" onSubmit={submit}>
            <div className="wb-input-wrap">
              <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true">
                <Sparkles size={17} strokeWidth={1.8} />
              </span>
              <input
                className="wb-input wb-input--has-prefix"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder='Ask anything — e.g. "why did we choose Postgres?"'
                aria-label="Ask your team memory"
              />
            </div>
            <button className="wb-btn wb-btn--primary wb-btn--lg" type="submit">
              Ask <ArrowRight size={16} strokeWidth={1.9} />
            </button>
          </form>

          <div className="home-chips">
            {starters.map((q) => (
              <button key={q} className="home-chip" onClick={() => onAsk(q)}>{q}</button>
            ))}
          </div>
        </PageHeader>

        {/* ---- Metrics: animated counters ---- */}
        <motion.div className="home-metrics" variants={fadeUp}>
          {c
            ? metrics.map((m) => (
                <button key={m.lab} className="metric" onClick={() => m.go && onNavigate(m.go)}>
                  <span className="metric-num tnum"><CountUp value={m.num} /></span>
                  <span className="metric-lab">{m.lab}</span>
                  <ArrowUpRight className="metric-go" size={15} strokeWidth={1.8} />
                </button>
              ))
            : [0, 1, 2, 3].map((i) => <div key={i} className="metric wb-skeleton" style={{ height: 92 }} />)}
        </motion.div>

        {/* ---- Insight band: real-data visualization ---- */}
        <motion.div className="home-insight" variants={fadeUp}>
          <section className="insight-card">
            <div className="insight-head">
              <h3>Where memory comes from</h3>
            </div>
            {sources.length > 0 ? (
              <div className="donut-row">
                <Suspense fallback={<ChartFallback />}>
                  <SourceDonut sources={sources} total={totalDocs} srcColors={srcColors} trackColor={tc.track} />
                </Suspense>
                <div className="donut-legend">
                  {sources.filter((s) => s.n > 0).map((s) => (
                    <button key={s.source} className="legend-row" onClick={() => onNavigate(canAdmin ? 'sources' : 'timeline')}>
                      <span className="legend-dot" style={{ background: srcColors[s.source] || tc.muted }} />
                      <span className="legend-name">{SOURCE_LABEL[s.source] || s.source}</span>
                      <span className="legend-n tnum">{s.n}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="insight-empty">
                <Plug size={20} strokeWidth={1.7} />
                <p>No sources connected yet. Connect Slack or GitHub to start forming memory.</p>
                {canAdmin && (
                  <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => onNavigate('sources')}>
                    Connect a source <ArrowRight size={14} strokeWidth={1.9} />
                  </button>
                )}
              </div>
            )}
          </section>

          <section className="insight-card">
            <div className="insight-head">
              <h3>Resolution</h3>
            </div>
            {resolveTotal > 0 ? (
              <>
                <Suspense fallback={<ChartFallback />}>
                  <ResolutionGauge pct={resolvePct} accent={tc.accent} trackColor={tc.track} />
                </Suspense>
                <div className="gauge-split">
                  <span><b className="tnum">{c.decisions}</b> decided</span>
                  <span className="dot-sep" />
                  <span><b className="tnum">{c.open_questions}</b> open</span>
                </div>
              </>
            ) : (
              <div className="insight-empty">
                <GitCommitHorizontal size={20} strokeWidth={1.7} />
                <p>No decisions tracked yet. Memory forms them as documents arrive.</p>
              </div>
            )}
          </section>

          <section className="insight-card">
            <div className="insight-head">
              <h3>Coverage &amp; access</h3>
            </div>
            <div className="cov-list">
              <div className="cov-row">
                <Calendar size={15} strokeWidth={1.8} />
                <span className="tnum">{coverageStart ? `${coverageStart} — today` : 'building memory…'}</span>
              </div>
              <div className="cov-row">
                <ShieldCheck size={15} strokeWidth={1.8} />
                <span>{c ? `${c.decisions} decisions · ${c.documents} documents` : '—'}</span>
              </div>
              <div className="cov-row">
                <RefreshCw size={15} strokeWidth={1.8} />
                <span>{user?.display_name || 'You'} · {role}</span>
              </div>
              <button className="cov-more" onClick={() => onNavigate(canAdmin ? 'settings' : 'people')}>
                {canAdmin ? 'Manage team' : 'See people & systems'} <ArrowRight size={14} strokeWidth={1.9} />
              </button>
            </div>
          </section>
        </motion.div>

        {/* ---- Relitigation alert ---- */}
        {revisit && (
          <motion.div className="home-relit" variants={fadeUp} role="status">
            <span className="relit-ico"><RotateCcw size={17} strokeWidth={1.9} /></span>
            <div>
              <b>A settled decision is being relitigated.</b>{' '}
              <a onClick={() => onNavigate('decisions', { decisionId: revisit.old_id })}>
                “{revisit.old_title}”
              </a>{' '}
              is revisited by “{revisit.new_title}” ({revisit.when}).
            </div>
          </motion.div>
        )}

        {/* ---- Setup checklist (admins, until complete) ---- */}
        <motion.div variants={fadeUp}>
          <SetupChecklist
            setup={setup}
            canAdmin={canAdmin}
            onNavigate={onNavigate}
            onInvite={onInvite}
            onAsk={onAsk}
          />
        </motion.div>

        {/* ---- Recent decisions / Open questions ---- */}
        <motion.div className="home-lists" variants={fadeUp}>
          <section className="home-list">
            <h3>Recent decisions</h3>
            {!stats && [0, 1, 2].map((i) => <div key={i} className="hl-row"><div className="wb-skeleton" style={{ height: 14, flex: 1 }} /></div>)}
            {stats && stats.recent_decisions.length === 0 && (
              <div className="hl-row"><span className="hl-title hl-muted">No decisions yet — add documents and memory forms them automatically.</span></div>
            )}
            {stats && stats.recent_decisions.map((d) => (
              <div className="hl-row" key={d.id}>
                <StatusBadge status={d.status} />
                <span className="hl-title hl-link" onClick={() => onNavigate('decisions', { decisionId: d.id })}>{d.title}</span>
                <span className="hl-date tnum">{d.date || ''}</span>
              </div>
            ))}
            {stats && stats.recent_decisions.length > 0 && (
              <button className="hl-more" onClick={() => onNavigate('decisions')}>View all decisions <ArrowRight size={14} strokeWidth={1.9} /></button>
            )}
          </section>

          <section className="home-list">
            <h3>Open questions</h3>
            {!stats && [0, 1, 2].map((i) => <div key={i} className="hl-row"><div className="wb-skeleton" style={{ height: 14, flex: 1 }} /></div>)}
            {stats && stats.open_questions.length === 0 && stats.stale_questions?.length === 0 && (
              <div className="hl-row"><span className="hl-title hl-muted">No open questions right now.</span></div>
            )}
            {stats && stats.open_questions.map((q) => (
              <div className="hl-row" key={q.id}>
                <GitCommitHorizontal size={15} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
                <span className="hl-title">{q.title}</span>
                {q.date && <span className="hl-date tnum">{q.date}</span>}
              </div>
            ))}
            {stats && stats.stale_questions?.map((q) => (
              <div className="hl-row" key={`s-${q.id}`}>
                <Badge tone="warning" variant="soft" mono>{q.age_days}d</Badge>
                <span className="hl-title">{q.title}</span>
              </div>
            ))}
            {stats && (stats.open_questions.length > 0 || stats.stale_questions?.length > 0) && (
              <button className="hl-more" onClick={() => onAsk('What are the most important open questions right now?')}>Ask about these <ArrowRight size={14} strokeWidth={1.9} /></button>
            )}
          </section>
        </motion.div>
      </motion.div>
    </MotionConfig>
  )
}
