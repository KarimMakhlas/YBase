import React, { useState } from 'react'
import { Check, ChevronDown, ChevronRight, UserPlus, Plug, Sparkles } from 'lucide-react'

const COLLAPSE_KEY = 'sb:setupCollapsed'

// Persistent setup checklist (replaces the old dismissible banner). Shows only
// to admins/owners while setup is incomplete; disappears once every step is done.
export default function SetupChecklist({ setup, canAdmin, onNavigate, onInvite, onAsk }) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  if (!setup || setup.complete || !canAdmin) return null

  const s = setup.steps || {}
  const items = [
    { key: 'invited', icon: UserPlus, label: 'Invite your teammates', action: 'Invite', done: !!s.invited, run: onInvite },
    { key: 'context', icon: Plug, label: 'Connect a source or add documents', action: 'Connect', done: !!s.context, run: () => onNavigate('sources') },
    { key: 'asked', icon: Sparkles, label: 'Ask your first question', action: 'Ask', done: !!s.asked, run: () => onAsk('Why did we choose Postgres over MongoDB?') },
  ]
  const doneCount = items.filter((i) => i.done).length

  const toggle = () => {
    const next = !collapsed
    setCollapsed(next)
    try { localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0') } catch { /* ignore */ }
  }

  return (
    <section className="setup-card wb-reveal" style={{ '--i': 3 }}>
      <button className="setup-head" onClick={toggle} aria-expanded={!collapsed}>
        {collapsed ? <ChevronRight size={16} strokeWidth={1.9} /> : <ChevronDown size={16} strokeWidth={1.9} />}
        <b>Finish setting up your workspace</b>
        <span className="setup-progress tnum">{doneCount}/{items.length}</span>
      </button>
      {!collapsed && (
        <div className="setup-steps">
          {items.map((it) => {
            const Icon = it.icon
            return (
              <div className={`setup-step${it.done ? ' is-done' : ''}`} key={it.key}>
                <span className="setup-check">
                  {it.done ? <Check size={13} strokeWidth={2.6} /> : <Icon size={15} strokeWidth={1.8} />}
                </span>
                <span className="setup-label">{it.label}</span>
                {!it.done && (
                  <button className="wb-btn wb-btn--sm" type="button" onClick={it.run}>
                    {it.action}
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
