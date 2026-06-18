import React, { useEffect, useMemo, useState } from 'react'
import { Plus, Search, RotateCw, TriangleAlert, Check, Info } from 'lucide-react'
import {
  deleteSource, getGitHubInstallUrl, getJiraInstallUrl, getSlackInstallUrl,
  listSourceJobs, listSources, listSourceStreams, patchSourceStream, startSourceSync,
} from '../api.js'
import { formatDateTime as fmtDate } from '../format.js'
import { useToast } from './Toast.jsx'
import { SrcBadge, StatusBadge } from '../whybase/ui.jsx'

const ACTIVE_STATUSES = new Set(['pending', 'running', 'paused'])
const PROVIDERS = { slack: { unit: 'channels' }, jira: { unit: 'projects' }, github: { unit: 'repos' } }
const unitFor = (provider) => PROVIDERS[provider]?.unit || 'streams'

function statsText(stats = {}, provider = 'slack') {
  const docs = stats.documents || 0
  const dupes = stats.duplicates || 0
  const streams = stats.streams || 0
  return `${docs} docs, ${dupes} skipped, ${streams} ${unitFor(provider)}`
}

// Why a completed sync brought in nothing — shown when the last sync imported 0
// documents while streams are selected. WhyBase ingests discussion, not code.
const EMPTY_IMPORT_HINT = {
  github: 'These repos have no issues or pull requests in the sync window, so there was nothing to import. WhyBase reads issues and PRs — not code, commits, or files.',
  jira: 'These projects have no issues in the sync window, so there was nothing to import.',
  slack: 'These channels have no messages in the sync window — or the bot hasn’t been invited to them yet (run /invite in Slack).',
}

// Shown when a connector lacks backend OAuth secrets, so the button explains
// what to set instead of failing on click.
const SETUP_HINT = {
  slack: 'Add SLACK_CLIENT_ID, SLACK_CLIENT_SECRET and SLACK_SIGNING_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  jira: 'Add JIRA_CLIENT_ID and JIRA_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  github: 'Add GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
}

export default function Sources() {
  const [sources, setSources] = useState(null)
  const [activeId, setActiveId] = useState(null)
  const [streams, setStreams] = useState(null)
  const [jobs, setJobs] = useState(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const connections = sources?.connections || []
  const active = connections.find((c) => c.id === activeId) || connections[0] || null
  // Connectors whose backend OAuth secrets aren't set — surfaced once sources load.
  const missingConnectors = sources
    ? ['slack', 'jira', 'github'].filter((p) => sources.configured?.[p] === false)
    : []

  const loadSources = () =>
    listSources()
      .then((data) => {
        setSources(data)
        setActiveId((id) => id || data.connections?.[0]?.id || null)
      })
      .catch((e) => toast(`Failed to load sources: ${e.message}`))

  const loadDetails = (connectionId = active?.id) => {
    if (!connectionId) { setStreams([]); setJobs([]); return }
    Promise.all([listSourceStreams(connectionId), listSourceJobs(connectionId)])
      .then(([nextStreams, nextJobs]) => { setStreams(nextStreams); setJobs(nextJobs) })
      .catch((e) => toast(`Failed to load source details: ${e.message}`))
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    let touched = false
    for (const [provider, label] of [['slack', 'Slack'], ['jira', 'Jira'], ['github', 'GitHub']]) {
      const status = params.get(provider)
      if (status === 'connected') toast(`${label} connected`, 'success')
      if (status === 'error') toast(`${label} OAuth failed`)
      if (status) { params.delete(provider); touched = true }
    }
    if (touched) {
      const next = `${window.location.pathname}${params.toString() ? `?${params}` : ''}`
      window.history.replaceState({}, '', next)
    }
    loadSources()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active?.id) return undefined
    setStreams(null); setJobs(null)
    loadDetails(active.id)
    return undefined
  }, [active?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active?.id || !jobs?.some((j) => ACTIVE_STATUSES.has(j.status))) return undefined
    const t = setTimeout(() => { loadSources(); loadDetails(active.id) }, 3000)
    return () => clearTimeout(t)
  }, [active?.id, jobs]) // eslint-disable-line react-hooks/exhaustive-deps

  const filteredStreams = useMemo(() => {
    const list = streams || []
    const q = query.trim().toLowerCase()
    if (!q) return list
    return list.filter((s) => s.name.toLowerCase().includes(q) || s.external_id.toLowerCase().includes(q))
  }, [streams, query])

  const INSTALL = {
    slack: { label: 'Slack', get: getSlackInstallUrl },
    jira: { label: 'Jira', get: getJiraInstallUrl },
    github: { label: 'GitHub', get: getGitHubInstallUrl },
  }
  const connect = async (provider) => {
    if (busy) return
    setBusy(true)
    const { label, get } = INSTALL[provider]
    try {
      const res = await get()
      if (!res.configured) { toast(res.error || `${label} connector is not configured`); return }
      window.location.href = res.url
    } catch (e) {
      toast(`${label} install failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const toggleStream = async (stream) => {
    try {
      const next = await patchSourceStream(active.id, stream.id, { selected: !stream.selected })
      setStreams((list) => list.map((s) => (s.id === next.id ? next : s)))
      await loadSources()
    } catch (e) {
      toast(`Channel update failed: ${e.message}`)
    }
  }

  const runBackfill = async () => {
    if (!active?.id || busy) return
    setBusy(true)
    try {
      await startSourceSync(active.id, 90)
      toast('Backfill queued', 'success')
      await Promise.all([loadSources(), loadDetails(active.id)])
    } catch (e) {
      toast(`Backfill failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    if (!active?.id || !window.confirm(`Disconnect ${active.name}?`)) return
    try {
      await deleteSource(active.id)
      setActiveId(null); setStreams(null); setJobs(null)
      toast('Source disconnected', 'success')
      await loadSources()
    } catch (e) {
      toast(`Disconnect failed: ${e.message}`)
    }
  }

  const connectButtons = (variant = 'wb-btn--secondary') =>
    ['slack', 'jira', 'github'].map((p) => {
      // Unknown (before load) counts as ready so buttons don't flash "needs setup".
      const ready = sources?.configured?.[p] !== false
      return (
        <button
          key={p}
          className={`wb-btn ${variant} wb-btn--sm${ready ? '' : ' wb-btn--needs-setup'}`}
          onClick={() => connect(p)}
          disabled={busy || !ready}
          title={ready ? `Connect ${INSTALL[p].label}` : SETUP_HINT[p]}
        >
          <Plus size={14} strokeWidth={1.8} /> {INSTALL[p].label}
          {!ready && <span className="needs-setup-tag">needs setup</span>}
        </button>
      )
    })

  return (
    <div className="app-page app-page--wide wb-reveal">
      <div className="sources-head">
        <div>
          <div className="eyebrow">Workspace</div>
          <h1 className="page-h1">Sources</h1>
          <p className="page-lede">Connect Slack, Jira and GitHub to workspace memory. Pick the channels to remember and run a backfill.</p>
        </div>
        <div className="sources-connect" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>{connectButtons()}</div>
      </div>

      {missingConnectors.length > 0 && (
        <div className="source-alert source-alert--info">
          <Info size={16} strokeWidth={1.8} />
          {missingConnectors.map((p) => INSTALL[p].label).join(', ')} {missingConnectors.length > 1 ? 'need' : 'needs'} setup — add each connector’s <code>*_CLIENT_ID</code> and <code>*_CLIENT_SECRET</code> (plus <code>CONNECTOR_SECRET_KEY</code>) to the backend, then restart to enable {missingConnectors.length > 1 ? 'them' : 'it'}.
        </div>
      )}

      {!sources && (
        <div style={{ marginTop: 'var(--sp-5)', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 64, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      )}

      {sources && connections.length === 0 && (
        <div className="md-detail" style={{ marginTop: 'var(--sp-6)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 'var(--sp-4)', textAlign: 'center' }}>
          <b style={{ fontSize: 'var(--fs-md)' }}>No sources connected</b>
          <p className="page-lede" style={{ textAlign: 'center' }}>Connect a system and WhyBase will remember the decisions inside it.</p>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center' }}>{connectButtons('wb-btn--primary')}</div>
        </div>
      )}

      {connections.length > 0 && active && (
        <div className="master-detail" style={{ marginTop: 'var(--sp-6)' }}>
          <aside className="md-list">
            {connections.map((c) => (
              <button key={c.id} className={`source-card ${active.id === c.id ? 'active' : ''}`} onClick={() => setActiveId(c.id)}>
                <div className="source-card-top">
                  <SrcBadge provider={c.provider}>{c.provider}</SrcBadge>
                  <StatusBadge status={c.status} dot />
                </div>
                <span className="source-card-name">{c.name}</span>
                <span className="source-card-meta tnum">
                  {c.selected_count || 0} of {c.stream_count || 0} {unitFor(c.provider)} · synced {fmtDate(c.last_sync_at)}
                  {c.last_sync_at && c.last_sync_documents != null && (
                    c.last_sync_documents > 0
                      ? ` · ${c.last_sync_documents} imported`
                      : ' · nothing imported'
                  )}
                </span>
                {c.active_jobs > 0 && <span className="source-card-meta tnum">{c.active_jobs} active job{c.active_jobs > 1 ? 's' : ''}</span>}
                {c.last_error && <span className="source-card-err">{c.last_error}</span>}
              </button>
            ))}
          </aside>

          <section className="md-detail" key={active.id}>
            <div className="source-toolbar">
              <div className="source-toolbar-id">
                <SrcBadge provider={active.provider}>{active.provider}</SrcBadge>
                <div>
                  <h3>{active.name}</h3>
                  <span className="tnum">{active.external_workspace_id}</span>
                </div>
              </div>
              <div className="source-toolbar-actions">
                <div className="wb-input-wrap">
                  <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Search size={16} strokeWidth={1.8} /></span>
                  <input className="wb-input wb-input--has-prefix" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${unitFor(active.provider)}`} />
                </div>
                <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => loadDetails(active.id)}><RotateCw size={14} strokeWidth={1.8} /> Refresh</button>
                <button className="wb-btn wb-btn--primary wb-btn--sm" onClick={runBackfill} disabled={busy || !streams?.some((s) => s.selected)}>Backfill 90 days</button>
                <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={disconnect}>Disconnect</button>
              </div>
            </div>

            {active.last_error && <div className="source-alert"><TriangleAlert size={16} strokeWidth={1.8} /> {active.last_error}</div>}

            {active.last_sync_at && active.last_sync_documents === 0 && active.selected_count > 0 && !active.active_jobs && (
              <div className="source-alert" style={{ color: 'var(--text-secondary)', borderColor: 'var(--border)', background: 'var(--surface-inset)' }}>
                <Info size={16} strokeWidth={1.8} /> Last sync imported nothing. {EMPTY_IMPORT_HINT[active.provider] || 'There was no new content to import in the sync window.'}
              </div>
            )}

            <div className="dlabel" style={{ marginTop: 'var(--sp-5)' }}>{unitFor(active.provider)} · {(streams || []).filter((s) => s.selected).length} selected</div>
            <div className="stream-table">
              {!streams && <div className="wb-skeleton" style={{ height: 40 }} />}
              {streams && filteredStreams.length === 0 && <div className="md-empty" style={{ padding: '12px' }}>No {unitFor(active.provider)} found.</div>}
              {filteredStreams.map((s) => (
                <div key={s.id} className={`stream-row ${s.selected ? 'on' : ''}`} onClick={() => toggleStream(s)} role="button" tabIndex={0}>
                  <label className="wb-check" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={s.selected} onChange={() => toggleStream(s)} />
                    <span className="wb-check__box"><Check size={12} strokeWidth={3} /></span>
                  </label>
                  <span className="stream-name">{active.provider === 'slack' ? `#${s.name}` : s.name}</span>
                  <StatusBadge status={s.status} />
                  <span className="stream-meta tnum">
                    {active.provider === 'slack'
                      ? (s.metadata?.num_members ? `${s.metadata.num_members} members` : s.external_id)
                      : s.external_id}
                  </span>
                  <span className="stream-last tnum">{s.last_synced_at ? `synced ${fmtDate(s.last_synced_at)}` : '—'}</span>
                  {s.last_error && <span className="job-err" style={{ gridColumn: '1 / -1' }}>{s.last_error}</span>}
                </div>
              ))}
            </div>

            <div className="dlabel" style={{ marginTop: 'var(--sp-6)' }}>Sync jobs</div>
            <div className="jobs-list">
              {!jobs && <div className="wb-skeleton" style={{ height: 36 }} />}
              {jobs && jobs.length === 0 && <div className="md-empty" style={{ padding: '4px' }}>No jobs yet.</div>}
              {jobs && jobs.map((j) => (
                <div key={j.id} className={`job-row ${j.status === 'failed' ? 'bad' : ''}`}>
                  <StatusBadge status={j.status} dot />
                  <b>{j.kind}</b>
                  <span className="job-stats tnum">{statsText(j.stats, active.provider)}</span>
                  <span className="job-when tnum">{fmtDate(j.created_at)}</span>
                  {j.state?.current_stream && <span className="job-stream tnum">{active.provider === 'slack' ? '#' : ''}{j.state.current_stream}</span>}
                  {j.next_retry_at && <span className="job-stream tnum">retry {fmtDate(j.next_retry_at)}</span>}
                  {j.error && <span className="job-err">{j.error}</span>}
                </div>
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
