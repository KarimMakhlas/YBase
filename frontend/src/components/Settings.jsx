import React, { useEffect, useState } from 'react'
import { Copy, KeyRound } from 'lucide-react'
import { createApiKey, listApiKeys, revokeApiKey } from '../api.js'
import { useToast } from './Toast.jsx'

export default function Settings() {
  const [keys, setKeys] = useState([])
  const [name, setName] = useState('')
  const [created, setCreated] = useState(null)
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const load = () => listApiKeys().then(setKeys).catch((e) => toast(`Could not load API keys: ${e.message}`))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const create = async () => {
    if (!name.trim() || busy) return
    setBusy(true)
    try { const key = await createApiKey(name.trim()); setCreated(key); setName(''); await load() } catch (e) { toast(`Could not create key: ${e.message}`) } finally { setBusy(false) }
  }
  const revoke = async (key) => {
    if (!window.confirm(`Revoke ${key.name}?`)) return
    try { await revokeApiKey(key.id); await load() } catch (e) { toast(`Could not revoke key: ${e.message}`) }
  }
  return <div className="page-shell"><h1>Settings</h1><section className="settings-section"><h3>Agent API keys</h3><p className="settings-sub">Use these keys with the MCP server or agent API. New keys are shown only once.</p>
    <div className="settings-form"><input className="wb-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Key name" /><button className="wb-btn wb-btn--primary" onClick={create} disabled={busy || !name.trim()}><KeyRound size={15} /> Create key</button></div>
    {created && <div className="invite-row"><code>{created.token}</code><button className="wb-btn wb-btn--ghost" onClick={() => navigator.clipboard.writeText(created.token)}><Copy size={14} /> Copy</button></div>}
    <div className="invite-list">{keys.filter((key) => !key.revoked_at).map((key) => <div className="invite-row" key={key.id}><span className="invite-main"><b>{key.name}</b><small>{key.token_prefix}…</small></span><button className="wb-btn wb-btn--ghost" onClick={() => revoke(key)}>Revoke</button></div>)}</div>
  </section></div>
}
