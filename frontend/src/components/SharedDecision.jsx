import React, { useEffect, useState } from 'react'
import { getSharedDecision } from '../api.js'

// Public, unauthenticated read-only view of one shared decision. Rendered
// standalone (no app shell) and ends with a sign-up call to action.
export default function SharedDecision({ token }) {
  const [state, setState] = useState({ loading: true })

  useEffect(() => {
    let alive = true
    getSharedDecision(token)
      .then((data) => { if (alive) setState({ loading: false, ...data }) })
      .catch(() => { if (alive) setState({ loading: false, error: true }) })
    return () => { alive = false }
  }, [token])

  if (state.loading) {
    return (
      <div className="shared-page">
        <div className="shared-card"><div className="skeleton skel-row" /></div>
      </div>
    )
  }

  if (state.error || !state.decision) {
    return (
      <div className="shared-page">
        <div className="shared-card">
          <div className="auth-mark">WhyBase</div>
          <h1>Decision unavailable</h1>
          <p>This share link is invalid or has been revoked.</p>
          <a className="primary-btn" href="#/home">Go to WhyBase</a>
        </div>
      </div>
    )
  }

  const d = state.decision
  return (
    <div className="shared-page">
      <div className="shared-card">
        <div className="shared-head">
          <span className="auth-mark">WhyBase</span>
          <span className="shared-from">Shared from {state.workspace_name}</span>
        </div>

        <div className="shared-status">
          <span className={`status-pill st-${d.status}`}>{d.status}</span>
          {d.date && <span className="tl-date">{d.date}</span>}
        </div>
        <h1>{d.title}</h1>
        {d.summary && <p className="shared-summary">{d.summary}</p>}

        {d.positions?.length > 0 && (
          <section>
            <h3>Positions &amp; reasoning</h3>
            <ul className="dc-positions">
              {d.positions.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          </section>
        )}
        {d.alternatives_considered?.length > 0 && (
          <p><strong>Alternatives considered:</strong> {d.alternatives_considered.join(' · ')}</p>
        )}
        {d.made_by?.length > 0 && (
          <p><strong>People:</strong> {d.made_by.join(', ')}</p>
        )}
        {d.topics?.length > 0 && (
          <div className="shared-topics">
            {d.topics.map((t) => <span key={t} className="topic-chip">{t}</span>)}
          </div>
        )}
        {d.sources?.length > 0 && (
          <section>
            <h3>Sources</h3>
            <ul className="shared-sources">
              {d.sources.map((s, i) => (
                <li key={i}>
                  <span className={`source-badge src-${s.source}`}>{s.source}</span>
                  {' '}{s.title}{s.author ? ` · ${s.author}` : ''}{s.date ? ` · ${s.date}` : ''}
                </li>
              ))}
            </ul>
          </section>
        )}

        <div className="shared-cta">
          <strong>This is one decision from {state.workspace_name}’s memory.</strong>
          <p>WhyBase remembers your team’s decisions, reasoning, and history — and answers “why” with citations.</p>
          <a className="primary-btn" href="#/home">Create your own team memory →</a>
        </div>
      </div>
    </div>
  )
}
