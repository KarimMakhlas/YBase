import React, { useEffect, useState } from 'react'
import { Check, Minus, X } from 'lucide-react'
import { getAnalyticsOverview, getMemoryQuality } from '../api.js'
import { useToast } from './Toast.jsx'
import { Badge } from '../whybase/ui.jsx'

const RANGES = [7, 30, 90]
const CHECK_ICON = {
  ok: <Check size={13} strokeWidth={2.4} />,
  warn: <Minus size={13} strokeWidth={2.4} />,
  fail: <X size={13} strokeWidth={2.4} />,
}

function MiniChart({ title, total, values, color }) {
  const max = Math.max(1, ...values)
  const n = Math.max(1, values.length)
  const W = 280
  const H = 56
  const gap = n > 60 ? 0.5 : 1
  const bw = (W - gap * (n - 1)) / n
  return (
    <div className="an-chart">
      <div className="an-chart-head">
        <span>{title}</span>
        <strong className="tnum">{total}</strong>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="an-spark" preserveAspectRatio="none" aria-hidden="true">
        {values.map((v, i) => {
          const h = (v / max) * (H - 2)
          return <rect key={i} x={i * (bw + gap)} y={H - h} width={Math.max(0.5, bw)} height={h} rx="1" fill={color} />
        })}
      </svg>
    </div>
  )
}

export default function Analytics() {
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [quality, setQuality] = useState(null)
  const toast = useToast()

  useEffect(() => {
    setData(null)
    getAnalyticsOverview(days).then(setData).catch((e) => toast(`Failed to load analytics: ${e.message}`))
  }, [days, toast])

  useEffect(() => {
    getMemoryQuality().then(setQuality).catch((e) => toast(`Failed to load memory health: ${e.message}`))
  }, [toast])

  const Header = () => (
    <div className="sources-head">
      <div>
        <div className="eyebrow">Workspace</div>
        <h1 className="page-h1">Analytics</h1>
        <p className="page-lede">Usage and activation signals for {data?.workspace?.name || 'the workspace'}.</p>
      </div>
      <div className="sources-connect" style={{ paddingTop: 26 }}>
        <div className="wb-tabs wb-tabs--segmented">
          {RANGES.map((r) => (
            <button key={r} className={`wb-tab ${r === days ? 'is-active' : ''}`} onClick={() => setDays(r)}>{r}d</button>
          ))}
        </div>
      </div>
    </div>
  )

  if (!data) {
    return (
      <div className="app-page app-page--wide">
        <Header />
        <div style={{ marginTop: 'var(--sp-5)', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 64, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      </div>
    )
  }

  const s = data.summary
  const ts = data.timeseries
  const cards = [
    { label: 'Members', value: s.members },
    { label: 'Active · 7d', value: s.active_7d },
    { label: 'Active · 30d', value: s.active_30d },
    { label: 'Questions asked', value: s.questions },
    { label: 'Documents', value: s.documents },
    { label: 'Decisions', value: s.decisions },
  ]
  const sum = (key) => ts.reduce((a, b) => a + b[key], 0)
  const peak = (key) => Math.max(0, ...ts.map((t) => t[key]))

  return (
    <div className="app-page app-page--wide wb-reveal">
      <Header />

      <div className="stat-grid an-cards">
        {cards.map((c) => (
          <div key={c.label} className="stat-card" style={{ cursor: 'default' }}>
            <span className="num tnum">{c.value}</span>
            <span className="lab">{c.label}</span>
          </div>
        ))}
      </div>

      <div className="section-label">Activation</div>
      <div className="ops-card">
        <div className="ops-steps">
          {data.activation.steps.map((st) => (
            <div className={`ops-step ${st.complete ? 'done' : ''}`} key={st.key}>
              <span className="ops-check">{st.complete ? <Check size={13} strokeWidth={2.4} /> : <Minus size={13} strokeWidth={2.4} />}</span>
              <div className="ops-step-main"><b>{st.label}</b></div>
            </div>
          ))}
        </div>
      </div>

      <div className="section-label">Last {days} days</div>
      <div className="an-charts">
        <MiniChart title="Documents ingested" total={sum('docs')} values={ts.map((t) => t.docs)} color="var(--accent)" />
        <MiniChart title="Questions asked" total={sum('questions')} values={ts.map((t) => t.questions)} color="var(--success)" />
        <MiniChart title="Active users (peak)" total={peak('active_users')} values={ts.map((t) => t.active_users)} color="var(--warning)" />
      </div>

      {quality && (
        <>
          <div className="section-label" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            Memory health
            <Badge tone={quality.healthy ? 'success' : 'warning'} variant="soft" mono dot>{quality.healthy ? 'healthy' : 'needs attention'}</Badge>
          </div>
          <div className="ops-card">
            <ul className="an-checks">
              {quality.checks.map((c) => (
                <li key={c.key} className={`an-check-${c.status}`}>
                  <span className="an-check-mark">{CHECK_ICON[c.status] || CHECK_ICON.warn}</span>
                  <span className="an-check-label">{c.label}</span>
                  <span className="an-check-detail">{c.detail}</span>
                </li>
              ))}
            </ul>
            {quality.topicless.length > 0 && (
              <div className="an-quality-list"><strong>Decisions missing topics:</strong> {quality.topicless.map((d) => d.label).join(' · ')}</div>
            )}
            {quality.low_confidence.length > 0 && (
              <div className="an-quality-list"><strong>Low-confidence decisions:</strong> {quality.low_confidence.map((d) => `${d.label} (${Math.round(d.confidence * 100)}%)`).join(' · ')}</div>
            )}
          </div>
        </>
      )}

      <div className="section-label">Member engagement</div>
      <div className="an-table">
        <div className="an-row an-row-head">
          <span>Member</span><span>Role</span><span>Questions</span><span>Active days</span><span>Last active</span>
        </div>
        {data.members.map((m) => (
          <div key={m.id} className="an-row">
            <span className="an-member"><strong>{m.display_name}</strong><em>{m.email}</em></span>
            <span><Badge tone="neutral" variant="soft" mono>{m.role}</Badge></span>
            <span className="tnum">{m.questions_asked}</span>
            <span className="tnum">{m.active_days}</span>
            <span className="tnum">{m.last_active ? String(m.last_active).slice(0, 10) : '—'}{m.disabled ? ' · disabled' : ''}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
