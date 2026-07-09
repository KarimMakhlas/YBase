import React, { useState } from 'react'
import { ArrowRight, Check, Copy, Database, Plug } from 'lucide-react'
import {
  createWorkspace,
  createWorkspaceInvite,
  seedDemoData,
  getJiraInstallUrl,
  getGitHubInstallUrl,
  getSlackInstallUrl,
  getNotionInstallUrl,
  completeOnboarding,
} from '../api.js'

const STEPS = ['Workspace', 'Invite', 'Connect']

// Post-signup setup wizard. Rendered while the user has no workspace yet (or is
// mid-onboarding). Step 1 creates the workspace (making the user its owner);
// steps 2–3 are skippable and just kick-start invites and context.
export default function Onboarding({ user, onWorkspaceCreated, onFinish, onLogout }) {
  const [step, setStep] = useState(user.workspace ? 1 : 0)
  const [wsName, setWsName] = useState('')
  const [emails, setEmails] = useState('')
  const [inviteLink, setInviteLink] = useState('')
  const [invitedCount, setInvitedCount] = useState(0)
  const [failedInvites, setFailedInvites] = useState([])
  const [copied, setCopied] = useState(false)
  const [sampleLoaded, setSampleLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const goStep = (n) => {
    setStep(n)
    setError('')
  }

  const createWs = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const payload = await createWorkspace(wsName.trim())
      onWorkspaceCreated(payload) // keeps us in the wizard, now with a workspace
      goStep(1)
    } catch (err) {
      setError(friendly(err.message))
    } finally {
      setBusy(false)
    }
  }

  const makeLink = async () => {
    const inv = await createWorkspaceInvite({ role: 'member' })
    const link = `${window.location.origin}${inv.path}`
    setInviteLink(link)
    return link
  }

  const copyLink = async () => {
    setBusy(true)
    setError('')
    try {
      const link = inviteLink || (await makeLink())
      try {
        await navigator.clipboard.writeText(link)
        setCopied(true)
        setTimeout(() => setCopied(false), 1600)
      } catch {
        /* clipboard blocked (no focus/permission) — link is shown for manual copy */
      }
    } catch (err) {
      setError(friendly(err.message))
    } finally {
      setBusy(false)
    }
  }

  const sendInvites = async () => {
    const list = emails
      .split(/[\s,;]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (!list.length) {
      goStep(2)
      return
    }
    setBusy(true)
    setError('')
    try {
      let n = 0
      const failed = []
      for (const email of list) {
        try {
          await createWorkspaceInvite({ role: 'member', email })
          n += 1
        } catch {
          failed.push(email)
        }
      }
      setInvitedCount((prev) => prev + n)
      setFailedInvites(failed)
      // Only advance if everything went out — otherwise stay so they can see what
      // failed, and leave just those addresses in the box so a retry doesn't
      // re-invite the ones that already went through.
      setEmails(failed.join(', '))
      if (!failed.length) goStep(2)
    } finally {
      setBusy(false)
    }
  }

  const connect = async (provider) => {
    setBusy(true)
    setError('')
    try {
      const fn = {
        jira: getJiraInstallUrl,
        github: getGitHubInstallUrl,
        slack: getSlackInstallUrl,
        notion: getNotionInstallUrl,
      }[provider]
      const res = await fn()
      if (!res.configured) {
        setError(res.error || 'This connector is not configured on this instance yet.')
        setBusy(false)
        return
      }
      window.location.href = res.url // OAuth redirect; user returns to the app
    } catch (err) {
      setError(friendly(err.message))
      setBusy(false)
    }
  }

  const loadSample = async () => {
    setBusy(true)
    setError('')
    try {
      await seedDemoData()
      setSampleLoaded(true)
    } catch (err) {
      setError(friendly(err.message))
    } finally {
      setBusy(false)
    }
  }

  const finish = async () => {
    setBusy(true)
    try {
      await completeOnboarding()
    } catch {
      /* non-fatal — they still enter the workspace */
    }
    setBusy(false)
    onFinish()
  }

  return (
    <div className="auth-page">
      <div className="auth-card onb-card">
        <div className="onb-head">
          <div className="auth-mark">YBase</div>
          <button type="button" className="linkbtn" onClick={onLogout}>
            Log out
          </button>
        </div>

        <div className="onb-steps">
          {STEPS.map((label, i) => (
            <div
              key={label}
              className={`onb-dot${i === step ? ' is-active' : ''}${i < step ? ' is-done' : ''}`}
            >
              <span className="onb-dot-num">{i < step ? <Check size={12} strokeWidth={2.4} /> : i + 1}</span>
              {label}
            </div>
          ))}
        </div>

        {step === 0 && (
          <form className="onb-body" onSubmit={createWs}>
            <h1>Name your workspace</h1>
            <p>This is the shared memory your team builds together. You can rename it later.</p>
            <label className="field">
              <span>Workspace name</span>
              <input
                value={wsName}
                onChange={(e) => setWsName(e.target.value)}
                placeholder="Acme Engineering"
                autoFocus
                required
              />
            </label>
            <div className="onb-actions onb-actions--end">
              <button type="submit" className="wb-btn wb-btn--primary" disabled={busy}>
                {busy ? 'Creating…' : 'Create workspace'} <ArrowRight size={15} strokeWidth={1.9} />
              </button>
            </div>
          </form>
        )}

        {step === 1 && (
          <div className="onb-body">
            <h1>Invite your teammates</h1>
            <p>
              YBase gets better with your team. Add emails (commas or new lines), or share an
              invite link. You can do this later, too.
            </p>
            <label className="field">
              <span>Teammate emails</span>
              <textarea
                rows={3}
                value={emails}
                onChange={(e) => setEmails(e.target.value)}
                placeholder="alex@acme.com, sam@acme.com"
              />
            </label>
            <div className="onb-linkrow">
              <button type="button" className="wb-btn" onClick={copyLink} disabled={busy}>
                <Copy size={14} strokeWidth={1.9} /> {copied ? 'Link copied' : 'Copy invite link'}
              </button>
              {inviteLink && <code className="onb-link">{inviteLink}</code>}
            </div>
            {invitedCount > 0 && (
              <div className="onb-note">
                Invited {invitedCount} teammate{invitedCount > 1 ? 's' : ''}.
              </div>
            )}
            {failedInvites.length > 0 && (
              <div className="onb-note onb-note--warn">
                Couldn’t send to {failedInvites.length} address{failedInvites.length > 1 ? 'es' : ''}:{' '}
                {failedInvites.join(', ')}. Check {failedInvites.length > 1 ? 'them' : 'it'} and try again, or skip for now.
              </div>
            )}
            <div className="onb-actions">
              <button type="button" className="linkbtn" onClick={() => goStep(2)}>
                Skip for now
              </button>
              <button type="button" className="wb-btn wb-btn--primary" onClick={sendInvites} disabled={busy}>
                {emails.trim() ? 'Send invites' : 'Continue'} <ArrowRight size={15} strokeWidth={1.9} />
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="onb-body">
            <h1>Bring in your context</h1>
            <p>
              Connect a tool so YBase remembers the decisions inside it — or load sample data to
              explore first.
            </p>
            <div className="onb-tiles">
              <button type="button" className="onb-tile" onClick={() => connect('jira')} disabled={busy}>
                <Plug size={16} strokeWidth={1.8} /> Connect Jira
              </button>
              <button type="button" className="onb-tile" onClick={() => connect('github')} disabled={busy}>
                <Plug size={16} strokeWidth={1.8} /> Connect GitHub
              </button>
              <button type="button" className="onb-tile" onClick={() => connect('slack')} disabled={busy}>
                <Plug size={16} strokeWidth={1.8} /> Connect Slack
              </button>
              <button type="button" className="onb-tile" onClick={() => connect('notion')} disabled={busy}>
                <Plug size={16} strokeWidth={1.8} /> Connect Notion
              </button>
            </div>
            <button
              type="button"
              className="wb-btn onb-sample"
              onClick={loadSample}
              disabled={busy || sampleLoaded}
            >
              <Database size={14} strokeWidth={1.9} />
              {sampleLoaded ? 'Sample data loaded' : 'Load sample data'}
              {sampleLoaded && <Check size={14} strokeWidth={2.2} />}
            </button>
            <div className="onb-actions">
              <button type="button" className="linkbtn" onClick={finish} disabled={busy}>
                I’ll do this later
              </button>
              <button type="button" className="wb-btn wb-btn--primary" onClick={finish} disabled={busy}>
                {sampleLoaded ? 'Explore workspace' : 'Finish setup'} <ArrowRight size={15} strokeWidth={1.9} />
              </button>
            </div>
          </div>
        )}

        {error && <div className="auth-error">{error}</div>}
      </div>
    </div>
  )
}

function friendly(message) {
  if (/429/.test(message)) return 'Too many attempts — please wait a minute and try again.'
  if (/409/.test(message)) return 'That didn’t go through — please refresh and try again.'
  if (/40[13]/.test(message)) return 'You don’t have permission to do that.'
  return message || 'Something went wrong — please try again.'
}
