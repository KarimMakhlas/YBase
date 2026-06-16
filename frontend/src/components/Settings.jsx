import React, { useEffect, useState } from 'react'
import { Mail, Link2, UserPlus } from 'lucide-react'
import {
  createWorkspaceInvite, createWorkspaceUser, listWorkspaceInvites,
  listWorkspaceUsers, patchWorkspaceUser, revokeWorkspaceInvite,
} from '../api.js'
import { useToast } from './Toast.jsx'
import { Avatar, Badge } from '../whybase/ui.jsx'

const EMPTY = { email: '', display_name: '', password: '', role: 'member' }

function inviteUrl(path) {
  return `${window.location.origin}${path}`
}

export default function Settings({ auth }) {
  const [users, setUsers] = useState(null)
  const [form, setForm] = useState(EMPTY)
  const [busy, setBusy] = useState(false)
  const [invites, setInvites] = useState(null)
  const [inviteRole, setInviteRole] = useState('member')
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviting, setInviting] = useState(false)
  const toast = useToast()
  const canPatch = auth?.workspace?.role === 'owner'
  const wsName = auth?.workspace?.name || 'this workspace'

  const load = () => listWorkspaceUsers().then(setUsers).catch((e) => toast(`Failed to load users: ${e.message}`))
  const loadInvites = () => listWorkspaceInvites().then(setInvites).catch((e) => toast(`Failed to load invites: ${e.message}`))

  useEffect(() => { load(); loadInvites() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const copy = async (text) => {
    try {
      await navigator.clipboard.writeText(text)
      toast('Invite link copied', 'success')
    } catch {
      toast('Copy failed — select and copy manually')
    }
  }

  const generateInvite = async () => {
    if (inviting) return
    setInviting(true)
    try {
      const res = await createWorkspaceInvite({ role: inviteRole, email: inviteEmail.trim() || null })
      if (res.email?.status === 'sent') toast(`Invite emailed to ${inviteEmail.trim()}`, 'success')
      else await copy(inviteUrl(res.path))
      setInviteEmail('')
      await loadInvites()
    } catch (err) {
      toast(`Could not create invite: ${err.message}`)
    } finally {
      setInviting(false)
    }
  }

  const revokeInvite = async (id) => {
    try {
      await revokeWorkspaceInvite(id)
      toast('Invite revoked', 'success')
      await loadInvites()
    } catch (err) {
      toast(`Revoke failed: ${err.message}`)
    }
  }

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      await createWorkspaceUser(form)
      setForm(EMPTY)
      toast('User created', 'success')
      await load()
    } catch (err) {
      toast(`Create user failed: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  const patch = async (id, body) => {
    try {
      await patchWorkspaceUser(id, body)
      toast('User updated', 'success')
      await load()
    } catch (err) {
      toast(`Update failed: ${err.message}`)
    }
  }

  const activeInvites = (invites || []).filter((i) => i.state === 'active')

  return (
    <div className="app-page wb-reveal">
      <div className="eyebrow">Workspace</div>
      <h1 className="page-h1">Settings</h1>
      <p className="page-lede">Manage who can read and curate {wsName}’s memory.</p>

      <section className="settings-section wb-reveal" style={{ '--i': 1 }}>
        <h3>Invite teammates</h3>
        <p className="settings-sub">Share a link so teammates can join {wsName} themselves — no password to set for them.</p>
        <div className="settings-form">
          <div className="wb-input-wrap">
            <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Mail size={16} strokeWidth={1.8} /></span>
            <input className="wb-input wb-input--has-prefix" type="email" placeholder="Email (optional — sends the link)" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} />
          </div>
          <select className="wb-select" style={{ width: 130, flex: 'none' }} value={inviteRole} onChange={(e) => setInviteRole(e.target.value)} aria-label="Invite role">
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <button className="wb-btn wb-btn--primary" type="button" onClick={generateInvite} disabled={inviting}>
            <Link2 size={15} strokeWidth={1.8} /> {inviting ? 'Generating…' : inviteEmail.trim() ? 'Send invite' : 'Generate invite link'}
          </button>
        </div>
        <div className="invite-list">
          {invites && activeInvites.length === 0 && <div className="md-empty">No active invite links.</div>}
          {activeInvites.map((i) => (
            <div className="invite-row" key={i.id}>
              <Link2 size={15} strokeWidth={1.8} />
              <span className="invite-main">
                <b>{i.role} invite</b>
                {i.email && <small>{i.email}</small>}
              </span>
              <span className="invite-exp tnum">expires {String(i.expires_at).slice(0, 10)}</span>
              <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => revokeInvite(i.id)}>Revoke</button>
            </div>
          ))}
        </div>
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 2 }}>
        <h3>Add user directly</h3>
        <p className="settings-sub">Create an account with a temporary password the teammate changes on first sign-in.</p>
        <form className="settings-form" onSubmit={submit}>
          <input className="wb-input" placeholder="Email" type="email" value={form.email} onChange={set('email')} required style={{ flex: 1, minWidth: 180 }} />
          <input className="wb-input" placeholder="Display name" value={form.display_name} onChange={set('display_name')} required style={{ flex: 1, minWidth: 150 }} />
          <input className="wb-input" placeholder="Temporary password" type="password" minLength={12} value={form.password} onChange={set('password')} required style={{ flex: 1, minWidth: 150 }} />
          <select className="wb-select" style={{ width: 120, flex: 'none' }} value={form.role} onChange={set('role')}>
            <option value="member">member</option>
            <option value="admin">admin</option>
            <option value="owner">owner</option>
          </select>
          <button className="wb-btn wb-btn--secondary" type="submit" disabled={busy}>
            <UserPlus size={15} strokeWidth={1.8} /> {busy ? 'Creating…' : 'Create'}
          </button>
        </form>
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 3 }}>
        <h3>Members{users ? ` · ${users.length}` : ''}</h3>
        <div className="user-table">
          {!users && <div className="wb-skeleton" style={{ height: 48 }} />}
          {users && users.map((u) => (
            <div className="user-row" key={u.id}>
              <Avatar name={u.display_name} size="sm" />
              <span className="user-main">
                <b>{u.display_name}</b>
                <small>{u.email}</small>
              </span>
              <Badge tone={u.disabled ? 'neutral' : 'success'} variant="soft" mono dot>{u.disabled ? 'disabled' : 'active'}</Badge>
              {canPatch ? (
                <>
                  <span className="user-role-select">
                    <select className="wb-select" value={u.role} onChange={(e) => patch(u.id, { role: e.target.value })} disabled={u.role === 'owner'} aria-label={`Role for ${u.email}`}>
                      <option value="member">member</option>
                      <option value="admin">admin</option>
                      <option value="owner">owner</option>
                    </select>
                  </span>
                  <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => patch(u.id, { disabled: !u.disabled })} disabled={u.role === 'owner'}>
                    {u.disabled ? 'Enable' : 'Disable'}
                  </button>
                </>
              ) : (
                <Badge tone="neutral" variant="soft" mono>{u.role}</Badge>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
