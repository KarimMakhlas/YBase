import React, { useEffect, useMemo, useState } from 'react'
import { Search, GitCommitHorizontal, CircleHelp } from 'lucide-react'
import { listPeople, getPerson } from '../api.js'
import { useToast } from './Toast.jsx'
import { Avatar, Badge, StatusBadge } from '../whybase/ui.jsx'

// Person pages: everything someone advocated, decided, or raised — built from
// the involves / raised_by edges memory formation already extracts.
export default function People({ focus, onNavigate, onOpenDoc }) {
  const [people, setPeople] = useState(null)
  const [detail, setDetail] = useState(null)
  const [activeId, setActiveId] = useState(focus?.personId ?? null)
  const [q, setQ] = useState('')
  const toast = useToast()

  useEffect(() => {
    listPeople().then(setPeople).catch((e) => toast(`Failed to load people: ${e.message}`))
  }, [toast])

  useEffect(() => {
    if (focus?.personId != null) setActiveId(focus.personId)
  }, [focus])

  // auto-select the first person once the list arrives
  useEffect(() => {
    if (activeId == null && people && people.length) setActiveId(people[0].id)
  }, [people, activeId])

  useEffect(() => {
    if (activeId == null) { setDetail(null); return }
    setDetail(null)
    getPerson(activeId).catch(() => null).then((d) => d && setDetail(d))
  }, [activeId])

  const list = useMemo(
    () => (people || []).filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase())),
    [people, q],
  )
  const activeMeta = (people || []).find((p) => p.id === activeId)

  const Header = () => (
    <>
      <div className="eyebrow">Memory</div>
      <h1 className="page-h1">People</h1>
      <p className="page-lede">Who advocated what, decided what, and asked what — assembled from every source the team has connected.</p>
    </>
  )

  if (!people) {
    return (
      <div className="app-page app-page--wide">
        <Header />
        <div style={{ marginTop: 'var(--sp-6)', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[0, 1, 2].map((i) => <div key={i} className="wb-skeleton" style={{ height: 56, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      </div>
    )
  }

  if (!people.length) {
    return (
      <div className="app-page app-page--wide">
        <Header />
        <div className="md-empty" style={{ marginTop: 'var(--sp-6)' }}>No people remembered yet — they appear once memory formation extracts who advocated and decided.</div>
      </div>
    )
  }

  return (
    <div className="app-page app-page--wide wb-reveal">
      <Header />
      <div className="master-detail">
        <aside className="md-list">
          <div className="md-search">
            <div className="wb-input-wrap">
              <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Search size={16} strokeWidth={1.8} /></span>
              <input className="wb-input wb-input--has-prefix" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a person" />
            </div>
          </div>
          {list.map((p) => (
            <button key={p.id} className={`person-row ${p.id === activeId ? 'active' : ''}`} onClick={() => setActiveId(p.id)}>
              <Avatar name={p.name} size="sm" />
              <span className="person-main">
                <b>{p.name}</b>
                {p.role && <small>{p.role}</small>}
              </span>
              <span className="person-counts tnum">
                {p.decisions || 0}<GitCommitHorizontal size={12} strokeWidth={1.8} />
                {p.questions || 0}<CircleHelp size={12} strokeWidth={1.8} />
              </span>
            </button>
          ))}
        </aside>

        <section className="md-detail" key={activeId}>
          {activeId != null && !detail && <div className="wb-skeleton" style={{ height: 120, borderRadius: 'var(--radius-md)' }} />}
          {detail && (
            <>
              <div className="person-head wb-reveal">
                <Avatar name={detail.name} size="lg" />
                <div>
                  <h2>{detail.name}</h2>
                  {(activeMeta?.role || detail.role) && <div className="person-role">{activeMeta?.role || detail.role}</div>}
                </div>
              </div>
              {detail.summary && <p className="person-summary wb-reveal" style={{ '--i': 1 }}>{detail.summary}</p>}

              <div className="dlabel" style={{ marginTop: 'var(--sp-6)' }}>Decisions on the record</div>
              {detail.decisions.length === 0 && <div className="md-empty">None recorded.</div>}
              <div className="person-positions">
                {detail.decisions.map((d, i) => (
                  <div className="position-card wb-reveal" style={{ '--i': i + 2 }} key={d.node_id} onClick={() => onNavigate('decisions', { decisionId: d.node_id })}>
                    <div className="position-head">
                      <StatusBadge status={d.status} />
                      <span className="position-title">{d.title}</span>
                      {d.date && <span className="position-date tnum">{d.date}</span>}
                    </div>
                    {d.positions.length > 0 && <p className="position-quote">“{d.positions.join('” · “')}”</p>}
                  </div>
                ))}
              </div>

              <div className="dlabel" style={{ marginTop: 'var(--sp-6)' }}>Questions raised</div>
              {detail.questions.length === 0 && <div className="md-empty">None recorded.</div>}
              <div className="person-questions">
                {detail.questions.map((qq) => (
                  <div className="q-row" key={qq.node_id}>
                    <CircleHelp size={15} strokeWidth={1.8} />
                    <span>{qq.title}</span>
                    {qq.status && <StatusBadge status={qq.status} />}
                  </div>
                ))}
              </div>

              {detail.documents.length > 0 && (
                <>
                  <div className="dlabel" style={{ marginTop: 'var(--sp-6)' }}>Appears in</div>
                  <div className="dsources">
                    {detail.documents.map((d) => (
                      <button key={d.document_id} className={`src-badge src-${d.source}`} style={{ cursor: 'pointer' }} onClick={() => onOpenDoc(d.document_id)}>
                        <i className="src-dot" aria-hidden="true" /> {d.source}: {d.title} ({d.date || '—'})
                      </button>
                    ))}
                  </div>
                </>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  )
}
