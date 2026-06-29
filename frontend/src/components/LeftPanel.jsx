import React, { useEffect, useMemo, useState } from 'react'
import {
  RotateCcw, GitCommitHorizontal, CircleHelp, CheckCircle2, FileText, Plug,
  ArrowRight, ArrowUpRight, AlertTriangle, Sparkles,
} from 'lucide-react'
import { motion, MotionConfig } from 'framer-motion'
import { getStats } from '../api.js'
import { useToast } from './Toast.jsx'
import { useThemeColors, SOURCE_VARS } from '../ybase/charts.js'
import { staggerContainer, fadeUp } from '../ybase/motionPresets.js'
import CountUp from '../ybase/CountUp.jsx'
import SetupChecklist from './SetupChecklist.jsx'
import '../ybase/home.css'

const SOURCE_LABEL = { slack: 'Slack', notion: 'Notion', github: 'GitHub', jira: 'Jira', meeting: 'Meeting' }

function greeting() {
  const h = new Date().getHours()
  if (h < 5) return 'Late night'
  if (h < 12) return 'Good morning'
  if (h < 18) return 'Good afternoon'
  return 'Good evening'
}

// A calm, glanceable left panel. Idle "Pulse": one health line, a few counters,
// where memory comes from, the relitigation hook, and a weekly change feed. The
// anti-overwhelm surface — everything deeper is one tap away in chat or the menu.
export default function LeftPanel({ canAdmin = false, workspace, user, setup, onAsk, onNavigate, onSelectView, onInvite }) {
  const [stats, setStats] = useState(null)
  const toast = useToast()
  const srcColors = useThemeColors(SOURCE_VARS)
  const tc = useThemeColors({ muted: '--text-tertiary' })

  // A rolling 7-day window powers the "what changed this week" digest.
  const load = React.useCallback(() => {
    const weekAgo = new Date(Date.now() - 7 * 864e5).toISOString()
    getStats(weekAgo)
      .then(setStats)
      .catch((e) => toast(`Couldn't load your memory pulse: ${e.message}`))
  }, [toast])

  // Refetch when the tab regains focus, so the pulse reflects memory that
  // formed in the background instead of going stale.
  useEffect(() => {
    load()
    const onWake = () => { if (document.visibilityState === 'visible') load() }
    window.addEventListener('focus', load)
    document.addEventListener('visibilitychange', onWake)
    return () => {
      window.removeEventListener('focus', load)
      document.removeEventListener('visibilitychange', onWake)
    }
  }, [load])

  const c = stats?.counts
  const sources = stats?.sources || []
  const revisit = stats?.revisits?.[0]
  const digest = stats?.digest
  const role = workspace?.role || 'member'
  const firstName = (user?.display_name || '').split(/\s+/)[0] || 'there'
  const totalDocs = sources.reduce((a, s) => a + (s.n || 0), 0)

  const healthLine = useMemo(() => {
    if (!c) return null
    const plural = (n, s) => `${n} ${s}${n === 1 ? '' : 's'}`
    if ((c.documents || 0) === 0) return 'Your memory is empty — connect a source to start remembering.'
    if ((c.open_questions || 0) === 0) return `All caught up — ${plural(c.decisions, 'decision')} remembered, nothing open.`
    return `${plural(c.decisions, 'decision')} remembered · ${c.open_questions} still open.`
  }, [c])

  const kpis = c
    ? [
        { num: c.documents, lab: 'Documents', go: () => (canAdmin ? onNavigate('sources') : onAsk('What documents are in our memory?')) },
        { num: c.decisions, lab: 'Decisions', go: () => onSelectView('decisions') },
        { num: c.open_questions, lab: 'Open', go: () => onAsk('What are the most important open questions right now?') },
      ]
    : []

  // Weekly change feed, newest-intent first: decisions, then resolutions, then
  // freshly-opened questions, capped so the panel never becomes a wall.
  const feed = useMemo(() => {
    if (!digest) return []
    const rows = []
    for (const d of digest.new_decisions || [])
      rows.push({ key: `d${d.id}`, icon: GitCommitHorizontal, tone: 'accent', text: d.title, lead: 'Decided', go: () => onNavigate('decisions', { decisionId: d.id }) })
    for (const q of digest.resolved_questions || [])
      rows.push({ key: `r${q.id}`, icon: CheckCircle2, tone: 'success', text: q.title, lead: 'Resolved', go: () => onAsk(`What was resolved about “${q.title}”?`) })
    for (const q of digest.opened_questions || [])
      rows.push({ key: `o${q.id}`, icon: CircleHelp, tone: 'warning', text: q.title, lead: 'New question', go: () => onAsk(`What's the latest on “${q.title}”?`) })
    return rows.slice(0, 7)
  }, [digest, onNavigate, onAsk])

  // Admin-only gentle nudges: questions left open too long, missing sources.
  const gaps = useMemo(() => {
    if (!canAdmin || !stats) return []
    const out = []
    if (totalDocs === 0) out.push({ key: 'nosrc', text: 'No sources connected yet', action: 'Connect', run: () => onNavigate('sources') })
    for (const q of (stats.stale_questions || []).slice(0, 3))
      out.push({ key: `s${q.id}`, text: q.title, badge: `${q.age_days}d open`, run: () => onAsk(`What's the status of “${q.title}”?`) })
    return out
  }, [canAdmin, stats, totalDocs, onNavigate, onAsk])

  if (!stats) {
    return (
      <div className="pulse">
        <div className="pulse-greet">
          <div className="wb-skeleton" style={{ height: 13, width: 120, marginBottom: 10 }} />
          <div className="wb-skeleton" style={{ height: 22, width: '80%' }} />
        </div>
        <div className="pulse-kpis">
          {[0, 1, 2].map((i) => <div key={i} className="wb-skeleton" style={{ height: 74, borderRadius: 'var(--radius-md)' }} />)}
        </div>
        <div className="wb-skeleton" style={{ height: 56, borderRadius: 'var(--radius-md)', marginTop: 'var(--sp-4)' }} />
        <div className="wb-skeleton" style={{ height: 180, borderRadius: 'var(--radius-md)', marginTop: 'var(--sp-4)' }} />
      </div>
    )
  }

  return (
    <MotionConfig reducedMotion="user">
      <motion.div className="pulse" variants={staggerContainer(0.06)} initial="hidden" animate="show">
        {/* ---- Greeting + one health line ---- */}
        <motion.div className="pulse-greet" variants={fadeUp}>
          <p className="pulse-hello">{greeting()}, {firstName}</p>
          <h2 className="pulse-health">{healthLine}</h2>
        </motion.div>

        {/* ---- Three counters ---- */}
        <motion.div className="pulse-kpis" variants={fadeUp}>
          {kpis.map((k) => (
            <button key={k.lab} className="pulse-kpi" onClick={k.go}>
              <span className="pulse-kpi-num tnum"><CountUp value={k.num} /></span>
              <span className="pulse-kpi-lab">{k.lab}</span>
              <ArrowUpRight className="pulse-kpi-go" size={14} strokeWidth={1.8} />
            </button>
          ))}
        </motion.div>

        {/* ---- Where memory comes from (slim stacked bar) ---- */}
        {totalDocs > 0 && (
          <motion.section className="pulse-sources" variants={fadeUp}>
            <div className="pulse-sources-bar" role="img" aria-label="Documents by source">
              {sources.filter((s) => s.n > 0).map((s) => (
                <span
                  key={s.source}
                  className="pulse-sources-seg"
                  title={`${SOURCE_LABEL[s.source] || s.source}: ${s.n}`}
                  style={{ width: `${(s.n / totalDocs) * 100}%`, background: srcColors[s.source] || tc.muted }}
                />
              ))}
            </div>
            <div className="pulse-sources-legend">
              {sources.filter((s) => s.n > 0).map((s) => (
                <button key={s.source} className="pulse-legend" onClick={() => onNavigate(canAdmin ? 'sources' : 'decisions')}>
                  <i style={{ background: srcColors[s.source] || tc.muted }} />
                  {SOURCE_LABEL[s.source] || s.source}
                  <b className="tnum">{s.n}</b>
                </button>
              ))}
            </div>
          </motion.section>
        )}

        {/* ---- Relitigation: the sharpest hook, kept prominent ---- */}
        {revisit && (
          <motion.button className="pulse-relit" variants={fadeUp} onClick={() => onNavigate('decisions', { decisionId: revisit.old_id })}>
            <span className="pulse-relit-ico"><RotateCcw size={16} strokeWidth={1.9} /></span>
            <span>
              <b>A settled decision is being relitigated.</b>{' '}
              “{revisit.old_title}” is revisited by “{revisit.new_title}” ({revisit.when}).
            </span>
            <ArrowRight className="pulse-relit-go" size={15} strokeWidth={1.9} />
          </motion.button>
        )}

        {/* ---- What changed this week ---- */}
        <motion.section className="pulse-card" variants={fadeUp}>
          <div className="pulse-card-head">
            <h3>What changed this week</h3>
            {digest?.new_documents > 0 && (
              <span className="pulse-pill"><FileText size={12} strokeWidth={1.9} /> {digest.new_documents} new</span>
            )}
          </div>
          {feed.length > 0 ? (
            <div className="pulse-feed">
              {feed.map((r) => {
                const Icon = r.icon
                return (
                  <button key={r.key} className="pulse-feed-row" onClick={r.go}>
                    <span className={`pulse-feed-ico tone-${r.tone}`}><Icon size={14} strokeWidth={1.9} /></span>
                    <span className="pulse-feed-text"><b>{r.lead}</b> {r.text}</span>
                  </button>
                )
              })}
            </div>
          ) : (
            <p className="pulse-empty">
              {totalDocs === 0
                ? 'Nothing yet — memory forms as your sources sync.'
                : 'Quiet week. Ask a question to dig into what’s already remembered.'}
            </p>
          )}
        </motion.section>

        {/* ---- Memory gaps (admins) ---- */}
        {gaps.length > 0 && (
          <motion.section className="pulse-card pulse-card--gaps" variants={fadeUp}>
            <div className="pulse-card-head">
              <h3><AlertTriangle size={14} strokeWidth={1.9} /> Needs attention</h3>
            </div>
            <div className="pulse-feed">
              {gaps.map((g) => (
                <button key={g.key} className="pulse-feed-row" onClick={g.run}>
                  <span className="pulse-feed-text">{g.text}</span>
                  {g.badge && <span className="pulse-gap-badge tnum">{g.badge}</span>}
                  {g.action && <span className="pulse-gap-action">{g.action} <ArrowRight size={12} strokeWidth={2} /></span>}
                </button>
              ))}
            </div>
          </motion.section>
        )}

        {/* ---- First-run setup checklist (admins, until complete) ---- */}
        <motion.div variants={fadeUp}>
          <SetupChecklist
            setup={setup}
            canAdmin={canAdmin}
            onNavigate={onNavigate}
            onInvite={onInvite}
            onAsk={onAsk}
          />
        </motion.div>

        {/* ---- Nudge to the conversation ---- */}
        <motion.button className="pulse-ask" variants={fadeUp} onClick={() => onAsk('What are our most recent decisions, and why?')}>
          <Sparkles size={15} strokeWidth={1.8} /> Ask your memory anything
        </motion.button>
      </motion.div>
    </MotionConfig>
  )
}
