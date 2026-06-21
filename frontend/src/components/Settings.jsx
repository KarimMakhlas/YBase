import React, { useEffect, useState } from 'react'
import { Mail, Link2, UserPlus, Crown } from 'lucide-react'
import {
  createWorkspaceInvite, createWorkspaceUser, listWorkspaceInvites,
  listWorkspaceUsers, patchWorkspaceUser, revokeWorkspaceInvite, transferOwnership,
} from '../api.js'
import { useToast } from './Toast.jsx'
import { Avatar, Badge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

const EMPTY = { email: '', display_name: '', password: '', role: 'member' }

// Plain-English description of what each workspace role can do.
const ROLE_BLURB = {
  owner: 'Full control — manage members, billing, and the only one who can transfer ownership.',
  admin: 'Manage members, invites, sources, and curate memory.',
  member: 'Read and search memory, ask questions, and give feedback.',
}

function inviteUrl(path) {
  return `${window.location.origin}${path}`
}

export default function Settings({ auth, onAuthChanged }) {
  const [users, setUsers] = useState(null)
  const [form, setForm] = useState(EMPTY)
  const [busy, setBusy] = useState(false)
  const [invites, setInvites] = useState(null)
  const [inviteRole, setInviteRole] = useState('member')
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviting, setInviting] = useState(false)
  const toast = useToast()
  const canPatch = auth?.workspace?.role === 'owner'
  const myRole = auth?.workspace?.role || 'member'
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

  // Disabling signs a member out and blocks their access — confirm first.
  // Re-enabling is harmless, so it goes straight through.
  const toggleDisabled = (u) => {
    if (!u.disabled && !window.confirm(`Disable ${u.display_name}? They’ll be signed out and lose access until you re-enable them.`)) return
    patch(u.id, { disabled: !u.disabled })
  }

  const transfer = async (userId, name) => {
    if (!window.confirm(
      `Make ${name} the owner of ${wsName}? You’ll become an admin and can no longer ` +
      `manage billing or transfer ownership.`
    )) return
    try {
      await transferOwnership(userId)
      toast(`${name} is now the owner`, 'success')
      await load()
      onAuthChanged?.() // our own role just changed to admin — refresh the shell
    } catch (err) {
      toast(`Transfer failed: ${err.message}`)
    }
  }

  const activeInvites = (invites || []).filter((i) => i.state === 'active')

  return (
    <div className="app-page wb-reveal">
      <PageHeader
        kicker="Settings"
        title={<>Control who sees <em>memory</em>.</>}
        lede={`Manage who can read and curate ${wsName}’s memory.`}
      />

      <div className="role-clarity">
        <Badge tone="accent" variant="soft" mono>your role · {myRole}</Badge>
        <span>{ROLE_BLURB[myRole]}</span>
      </div>

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
              {u.role === 'owner' ? (
                <Badge tone="accent" variant="soft" mono>owner</Badge>
              ) : canPatch ? (
                <>
                  <span className="user-role-select">
                    <select className="wb-select" value={u.role} onChange={(e) => patch(u.id, { role: e.target.value })} aria-label={`Role for ${u.email}`}>
                      <option value="member">member</option>
                      <option value="admin">admin</option>
                    </select>
                  </span>
                  <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => toggleDisabled(u)}>
                    {u.disabled ? 'Enable' : 'Disable'}
                  </button>
                  <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => transfer(u.id, u.display_name)} title="Make this member the owner" disabled={u.disabled}>
                    <Crown size={13} strokeWidth={1.9} /> Make owner
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
