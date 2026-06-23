import React, { useEffect, useRef, useState } from 'react'
import {
  GitCommitHorizontal, Plug, Settings as SettingsIcon, UserCircle2,
  UserPlus, Search, LogOut, ChevronsUpDown,
} from 'lucide-react'

// The single navigation surface for the chat-first shell: a circle (workspace
// avatar) in the top-right that opens a minimal menu to the few other places.
// Members see Decisions + Account; admins also get Sources/Settings/Invite.

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

  const go = (tab) => { setOpen(false); onNavigate(tab) }
  const h = hue(workspace?.name)

  return (
    <div className="acct" ref={ref}>
      <button
        className="acct-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Menu"
      >
        <span
          className="wb-avatar wb-avatar--sm"
          aria-hidden="true"
          style={{ background: `linear-gradient(150deg, hsl(${h} 52% 56%), hsl(${(h + 38) % 360} 54% 42%))` }}
        >
          {initials(workspace?.name)}
        </span>
        <ChevronsUpDown size={14} strokeWidth={1.8} className="acct-chev" aria-hidden="true" />
      </button>

      {open && (
        <div className="acct-menu" role="menu">
          <div className="acct-head">
            <b>{workspace?.name}</b>
            <small>{user?.display_name} · {role}</small>
          </div>
          <div className="acct-sep" />
          <button className="acct-item" role="menuitem" onClick={() => { setOpen(false); onSearch() }}>
            <Search size={16} strokeWidth={1.8} /> Search memory <span className="acct-kbd">⌘K</span>
          </button>
          <button className="acct-item" role="menuitem" onClick={() => go('decisions')}>
            <GitCommitHorizontal size={16} strokeWidth={1.8} /> Decisions
          </button>
          {isAdmin && (
            <button className="acct-item" role="menuitem" onClick={() => go('sources')}>
              <Plug size={16} strokeWidth={1.8} /> Sources
            </button>
          )}
          {isAdmin && (
            <button className="acct-item" role="menuitem" onClick={() => go('settings')}>
              <SettingsIcon size={16} strokeWidth={1.8} /> Settings
            </button>
          )}
          <button className="acct-item" role="menuitem" onClick={() => go('account')}>
            <UserCircle2 size={16} strokeWidth={1.8} /> Account
          </button>
          {isAdmin && (
            <button className="acct-item" role="menuitem" onClick={() => { setOpen(false); onInvite() }}>
              <UserPlus size={16} strokeWidth={1.8} /> Invite teammates
            </button>
          )}
          <div className="acct-sep" />
          <button className="acct-item acct-item--danger" role="menuitem" onClick={() => { setOpen(false); onLogout() }}>
            <LogOut size={16} strokeWidth={1.8} /> Log out
          </button>
        </div>
      )}
    </div>
  )
}
