import React, { useState } from 'react'
import { ArrowRight, Plug } from 'lucide-react'
import { completeOnboarding, createWorkspace, getGitHubInstallUrl, getNotionInstallUrl, getSlackInstallUrl } from '../api.js'

const CONNECTORS = {
  slack: getSlackInstallUrl,
  github: getGitHubInstallUrl,
  notion: getNotionInstallUrl,
}

export default function Onboarding({ user, onWorkspaceCreated, onFinish, onLogout }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const hasWorkspace = Boolean(user.workspace)

  const create = async (event) => {
    event.preventDefault()
    if (!name.trim()) return
    setBusy(true); setError('')
    try { onWorkspaceCreated(await createWorkspace(name.trim())) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const connect = async (provider) => {
    setBusy(true); setError('')
    try {
      const result = await CONNECTORS[provider]()
      if (!result.configured) throw new Error(result.error || 'Connector is not configured')
      window.location.href = result.url
    } catch (e) { setError(e.message); setBusy(false) }
  }
  const finish = async () => {
    setBusy(true); setError('')
    try { await completeOnboarding(); onFinish() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return <main className="auth-page"><section className="auth-card">
    {!hasWorkspace ? <form onSubmit={create}><h1>Create your workspace</h1><p>Name the shared memory your team will use.</p><input className="wb-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Workspace name" autoFocus />
      <button className="wb-btn wb-btn--primary" disabled={busy || !name.trim()}>Create workspace <ArrowRight size={15} /></button></form> : <>
      <h1>Connect your sources</h1><p>Start with the tools where your team works.</p><div className="onb-tiles">{Object.keys(CONNECTORS).map((provider) => <button key={provider} className="onb-tile" onClick={() => connect(provider)} disabled={busy}><Plug size={16} /> Connect {provider === 'github' ? 'GitHub' : provider[0].toUpperCase() + provider.slice(1)}</button>)}</div>
      <button className="wb-btn wb-btn--primary" onClick={finish} disabled={busy}>Finish setup <ArrowRight size={15} /></button></>}
    {error && <p className="auth-error">{error}</p>}<button className="linkbtn" onClick={onLogout}>Log out</button>
  </section></main>
}
