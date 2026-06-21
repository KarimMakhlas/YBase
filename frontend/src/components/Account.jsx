import React, { useState } from 'react'
import { ArrowLeft, Check, LogOut, ArrowRightLeft, DoorOpen, CreditCard } from 'lucide-react'
import { updateMe, logoutAll, leaveWorkspace, switchWorkspace } from '../api.js'
import { useToast } from './Toast.jsx'
import { Avatar, Badge } from '../whybase/ui.jsx'
import PageHeader from '../whybase/PageHeader.jsx'

// Personal account & profile — distinct from workspace Settings (which manages
// other people). Identity, password, the workspaces you belong to, and sessions.
export default function Account({ user, onAuthChanged, onNavigate, onBack }) {
  const toast = useToast()
  // `user` is the full auth payload: { user: {profile}, workspace, workspaces }.
  const profile = user.user || {}
  const [name, setName] = useState(profile.display_name || '')
  const [savingName, setSavingName] = useState(false)
  const [pw, setPw] = useState({ current: '', next: '' })
  const [savingPw, setSavingPw] = useState(false)
  const [busyWs, setBusyWs] = useState(null) // workspace id mid-action

  const isGoogle = profile.auth_provider === 'google'
  const activeWsId = user.workspace?.id
  const activeRole = user.workspace?.role
  const workspaces = user.workspaces || []

  const saveName = async (e) => {
    e.preventDefault()
    const trimmed = name.trim()
    if (!trimmed || trimmed === profile.display_name || savingName) return
    setSavingName(true)
    try {
      await updateMe({ display_name: trimmed })
      toast('Name updated', 'success')
      onAuthChanged?.()
    } catch (err) {
      toast(`Couldn’t update name: ${err.message}`)
    } finally {
      setSavingName(false)
    }
  }

  const changePassword = async (e) => {
    e.preventDefault()
    if (savingPw) return
    setSavingPw(true)
    try {
      await updateMe({ current_password: pw.current, new_password: pw.next })
      setPw({ current: '', next: '' })
      toast('Password changed — other devices signed out', 'success')
    } catch (err) {
      toast(/403/.test(err.message) ? 'Current password is incorrect' : `Couldn’t change password: ${err.message}`)
    } finally {
      setSavingPw(false)
    }
  }

  const doSwitch = async (id) => {
    setBusyWs(id)
    try {
      await switchWorkspace(id)
      onAuthChanged?.()
    } catch (err) {
      toast(`Couldn’t switch: ${err.message}`)
    } finally {
      setBusyWs(null)
    }
  }

  const doLeave = async (name) => {
    if (!window.confirm(`Leave ${name}? You’ll lose access until you’re invited back.`)) return
    setBusyWs(activeWsId)
    try {
      await leaveWorkspace()
      toast(`You’ve left ${name}`, 'success')
      onAuthChanged?.()
    } catch (err) {
      toast(/409/.test(err.message)
        ? 'Transfer ownership before leaving — a workspace must keep an owner.'
        : `Couldn’t leave: ${err.message}`)
    } finally {
      setBusyWs(null)
    }
  }

  const signOutEverywhere = async () => {
    if (!window.confirm('Sign out on all devices? You’ll need to sign in again here too.')) return
    try {
      await logoutAll()
      onAuthChanged?.() // session is gone → app drops to the sign-in screen
    } catch (err) {
      toast(`Couldn’t sign out everywhere: ${err.message}`)
    }
  }

  return (
    <div className="app-page wb-reveal">
      {onBack && (
        <button className="linkbtn" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--text-secondary)' }} onClick={onBack}>
          <ArrowLeft size={14} strokeWidth={1.8} /> Back
        </button>
      )}
      <PageHeader
        kicker="Account"
        title={<>Your profile, your <em>workspaces</em>.</>}
        lede="Manage your profile, password, and the workspaces you belong to."
      />

      <section className="settings-section wb-reveal" style={{ '--i': 1 }}>
        <h3>Profile</h3>
        <div className="account-identity">
          <Avatar name={profile.display_name} size="lg" />
          <div className="account-identity-meta">
            <span className="account-email">{profile.email}</span>
            <Badge tone={isGoogle ? 'accent' : 'neutral'} variant="soft" mono>
              {isGoogle ? 'Google sign-in' : 'Email & password'}
            </Badge>
          </div>
        </div>
        <form className="settings-form" onSubmit={saveName} style={{ marginTop: 'var(--sp-3, 14px)' }}>
          <input className="wb-input" value={name} onChange={(e) => setName(e.target.value)} aria-label="Display name" style={{ flex: 1, minWidth: 200 }} />
          <button className="wb-btn wb-btn--secondary" type="submit" disabled={savingName || !name.trim() || name.trim() === profile.display_name}>
            {savingName ? 'Saving…' : 'Save name'}
          </button>
        </form>
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 2 }}>
        <h3>Password</h3>
        {isGoogle ? (
          <p className="settings-sub">Your sign-in is managed by Google — there’s no password to change here.</p>
        ) : (
          <>
            <p className="settings-sub">Changing your password signs you out on every other device.</p>
            <form className="settings-form" onSubmit={changePassword}>
              <input className="wb-input" type="password" placeholder="Current password" value={pw.current} onChange={(e) => setPw((p) => ({ ...p, current: e.target.value }))} required style={{ flex: 1, minWidth: 160 }} />
              <input className="wb-input" type="password" placeholder="New password" minLength={12} value={pw.next} onChange={(e) => setPw((p) => ({ ...p, next: e.target.value }))} required style={{ flex: 1, minWidth: 160 }} />
              <button className="wb-btn wb-btn--secondary" type="submit" disabled={savingPw}>
                {savingPw ? 'Changing…' : 'Change password'}
              </button>
            </form>
          </>
        )}
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 3 }}>
        <h3>Workspaces</h3>
        <p className="settings-sub">
          Switch between workspaces or leave one.{' '}
          {activeWsId != null && (
            <button className="linkbtn" onClick={() => onNavigate?.('settings')}>Manage roles & ownership →</button>
          )}
        </p>
        <div className="account-ws-list">
          {workspaces.length === 0 && <div className="md-empty">You’re not in any workspace yet.</div>}
          {workspaces.map((w) => {
            const active = w.id === activeWsId
            return (
              <div className={active ? 'account-ws-row is-active' : 'account-ws-row'} key={w.id}>
                <Avatar name={w.name} size="sm" />
                <span className="account-ws-meta">
                  <b>{w.name}</b>
                  <small>{w.role}{active ? ' · current' : ''}</small>
                </span>
                {active ? (
                  <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => doLeave(w.name)} disabled={busyWs === w.id} title="Leave this workspace">
                    <DoorOpen size={14} strokeWidth={1.8} /> Leave
                  </button>
                ) : (
                  <button className="wb-btn wb-btn--sm wb-btn--ghost" onClick={() => doSwitch(w.id)} disabled={busyWs === w.id}>
                    <ArrowRightLeft size={14} strokeWidth={1.8} /> Switch
                  </button>
                )}
              </div>
            )
          })}
        </div>
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 4 }}>
        <h3>Billing</h3>
        <p className="settings-sub">
          {activeRole === 'owner'
            ? 'Manage the active workspace plan, trial status, and upgrade controls.'
            : 'Billing is managed by the workspace owner.'}
        </p>
        <button className="wb-btn wb-btn--secondary" onClick={() => onNavigate?.('plans')}>
          <CreditCard size={15} strokeWidth={1.8} /> {activeRole === 'owner' ? 'Manage billing' : 'View plan'}
        </button>
      </section>

      <section className="settings-section wb-reveal" style={{ '--i': 5 }}>
        <h3>Sessions</h3>
        <p className="settings-sub">Signed in on a shared or lost device? Sign out everywhere.</p>
        <button className="wb-btn wb-btn--secondary" onClick={signOutEverywhere}>
          <LogOut size={15} strokeWidth={1.8} /> Sign out all devices
        </button>
      </section>
    </div>
  )
}
