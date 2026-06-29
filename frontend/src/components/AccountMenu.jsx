import React, { useEffect, useRef, useState } from 'react'
import {
  UserCircle2, CreditCard, UserPlus, Search, LogOut, ChevronDown,
} from 'lucide-react'

// The workspace identity + menu, merged into one control at the top of the
// left panel: the avatar and name ARE the trigger. The menu holds only what
// isn't a page — search, account, billing, invites, log out.

function initials(name = '') {
  return (name || '?').split(/\s+/).slice(0, 2).map((w) => w[0] || '').join('').toUpperCase()
}
function hue(name = '') {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return h
}

export default function AccountMenu({ workspace, user, role, isAdmin, onNavigate, onInvite, onSearch, onLogout }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const run = (fn) => { setOpen(false); fn() }
  const go = (tab) => run(() => onNavigate(tab))
  const h = hue(workspace?.name)

  return (
    <div className="acct" ref={ref}>
      <button
        className="ws-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Workspace menu"
      >
        <span
          className="wb-avatar wb-avatar--sm"
          aria-hidden="true"
          style={{ background: `linear-gradient(150deg, hsl(${h} 52% 56%), hsl(${(h + 38) % 360} 54% 42%))` }}
        >
          {initials(workspace?.name)}
        </span>
        <span className="ws-trigger-name">{workspace?.name}</span>
        <ChevronDown size={15} strokeWidth={1.9} className="ws-trigger-chev" aria-hidden="true" />
      </button>

      {open && (
        <div className="acct-menu" role="menu">
          <div className="acct-head">
            <b>{workspace?.name}</b>
            <small>{user?.display_name} · {role}</small>
          </div>
          <div className="acct-sep" />
          <button className="acct-item" role="menuitem" onClick={() => run(onSearch)}>
            <Search size={16} strokeWidth={1.8} /> Search memory <span className="acct-kbd">⌘K</span>
          </button>
          <div className="acct-sep" />
          <button className="acct-item" role="menuitem" onClick={() => go('account')}>
            <UserCircle2 size={16} strokeWidth={1.8} /> Account
          </button>
          <button className="acct-item" role="menuitem" onClick={() => go('plans')}>
            <CreditCard size={16} strokeWidth={1.8} /> Billing &amp; plans
          </button>
          {isAdmin && (
            <button className="acct-item" role="menuitem" onClick={() => run(onInvite)}>
              <UserPlus size={16} strokeWidth={1.8} /> Invite teammates
            </button>
          )}
          <div className="acct-sep" />
          <button className="acct-item acct-item--danger" role="menuitem" onClick={() => run(onLogout)}>
            <LogOut size={16} strokeWidth={1.8} /> Log out
          </button>
        </div>
      )}
    </div>
  )
}
