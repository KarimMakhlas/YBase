import React, { useEffect, useMemo, useState, lazy, Suspense } from 'react'
import {
  Plus, Search, RotateCw, TriangleAlert, Check, Info, ChevronDown, Settings2,
  RefreshCw, CheckCheck, Square,
} from 'lucide-react'
import { motion, AnimatePresence, MotionConfig } from 'framer-motion'
import {
  deleteSource, getConfluenceInstallUrl, getDiscordInstallUrl, getFigmaInstallUrl,
  getGitHubInstallUrl, getGoogleDocsInstallUrl, getJiraInstallUrl, getLinearInstallUrl,
  getNotionInstallUrl, getSlackInstallUrl,
  listSourceJobs, listSources, listSourceStreams, patchSourceStream, startSourceSync,
  retrySourceJob, setFigmaTeam,
} from '../api.js'
import { formatDateTime as fmtDate } from '../format.js'
import { useToast } from './Toast.jsx'
import { SrcBadge, StatusBadge } from '../ybase/ui.jsx'
import { useThemeColors } from '../ybase/charts.js'
import { staggerContainer, fadeUp, ease } from '../ybase/motionPresets.js'
import CountUp from '../ybase/CountUp.jsx'
import PageHeader from '../ybase/PageHeader.jsx'
import ConnectorPickerModal from './ConnectorPickerModal.jsx'
import '../ybase/sources.css'

// Recharts is heavy — lazy-load (shared chunk with HomeCharts).
const SyncHealthGauge = lazy(() => import('./SourcesCharts.jsx').then((m) => ({ default: m.SyncHealthGauge })))
const RingFallback = () => <div className="wb-skeleton" style={{ width: 150, height: 150, borderRadius: '50%' }} />

const JOB_TONE = { complete: 'ok', failed: 'bad', paused: 'warn', running: 'run', pending: 'run' }

const ACTIVE_STATUSES = new Set(['pending', 'running', 'paused'])
const ALL_PROVIDERS = [
  'slack', 'jira', 'github', 'linear', 'notion', 'discord', 'confluence', 'googledocs', 'figma',
]
const PROVIDERS = {
  slack: { unit: 'channels' }, jira: { unit: 'projects' }, github: { unit: 'repos' },
  linear: { unit: 'teams' }, notion: { unit: 'pages' }, discord: { unit: 'channels' },
  confluence: { unit: 'spaces' }, googledocs: { unit: 'docs' }, figma: { unit: 'projects' },
}
const unitFor = (provider) => PROVIDERS[provider]?.unit || 'streams'
const shortDate = (iso) => { try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) } catch { return '' } }

// Why a completed sync brought in nothing — shown when the last sync imported 0
// documents while streams are selected. YBase ingests discussion, not code.
const EMPTY_IMPORT_HINT = {
  github: 'These repos have no issues or pull requests in the sync window, so there was nothing to import. YBase reads issues and PRs — not code, commits, or files.',
  jira: 'These projects have no issues in the sync window, so there was nothing to import.',
  slack: 'These channels have no messages in the sync window — or the bot hasn’t been invited to them yet (run /invite in Slack).',
  linear: 'These teams have no issues in the sync window, so there was nothing to import.',
  notion: 'These pages have no content in the sync window, so there was nothing to import.',
  discord: 'These channels have no messages in the sync window — or the bot hasn’t been added to them yet.',
  confluence: 'These spaces have no pages in the sync window, so there was nothing to import.',
  googledocs: 'No docs have changed in the sync window, so there was nothing to import.',
  figma: 'These files have no comments in the sync window — YBase reads design discussion, not file content.',
}

const SETUP_HINT = {
  slack: 'Add SLACK_CLIENT_ID, SLACK_CLIENT_SECRET and SLACK_SIGNING_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  jira: 'Add JIRA_CLIENT_ID and JIRA_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  github: 'Add GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  linear: 'Add LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  notion: 'Add NOTION_CLIENT_ID and NOTION_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  discord: 'Add DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and DISCORD_BOT_TOKEN (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  confluence: 'Add CONFLUENCE_CLIENT_ID and CONFLUENCE_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  googledocs: 'Add GOOGLE_DOCS_CLIENT_ID and GOOGLE_DOCS_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
  figma: 'Add FIGMA_CLIENT_ID and FIGMA_CLIENT_SECRET (plus CONNECTOR_SECRET_KEY) to the backend, then restart.',
}

export default function Sources() {
  const [sources, setSources] = useState(null)
  const [activeId, setActiveId] = useState(null)
  const [streams, setStreams] = useState(null)
  const [jobs, setJobs] = useState(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [manageOpen, setManageOpen] = useState(false)
  const [showAllJobs, setShowAllJobs] = useState(false)
  const [retryBusy, setRetryBusy] = useState(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [figmaTeamInput, setFigmaTeamInput] = useState('')
  const [figmaTeamBusy, setFigmaTeamBusy] = useState(false)
  const toast = useToast()

  const tc = useThemeColors({ accent: '--accent', track: '--border' })

  const connections = sources?.connections || []
  const active = connections.find((c) => c.id === activeId) || connections[0] || null
  const missingConnectors = sources
    ? ALL_PROVIDERS.filter((p) => sources.configured?.[p] === false)
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
    const providerLabels = [
      ['slack', 'Slack'], ['jira', 'Jira'], ['github', 'GitHub'], ['linear', 'Linear'],
      ['notion', 'Notion'], ['discord', 'Discord'], ['confluence', 'Confluence'],
      ['googledocs', 'Google Docs'], ['figma', 'Figma'],
    ]
    for (const [provider, label] of providerLabels) {
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
    setStreams(null); setJobs(null); setManageOpen(false); setShowAllJobs(false); setQuery('')
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

  const selectedStreams = useMemo(() => (streams || []).filter((s) => s.selected), [streams])

  const INSTALL = {
    slack: { label: 'Slack', get: getSlackInstallUrl },
    jira: { label: 'Jira', get: getJiraInstallUrl },
    github: { label: 'GitHub', get: getGitHubInstallUrl },
    linear: { label: 'Linear', get: getLinearInstallUrl },
    notion: { label: 'Notion', get: getNotionInstallUrl },
    discord: { label: 'Discord', get: getDiscordInstallUrl },
    confluence: { label: 'Confluence', get: getConfluenceInstallUrl },
    googledocs: { label: 'Google Docs', get: getGoogleDocsInstallUrl },
    figma: { label: 'Figma', get: getFigmaInstallUrl },
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

  // Figma can't list a user's teams, so after OAuth the team id is pasted
  // manually (from figma.com/files/team/<id>/...) before streams exist.
  const saveFigmaTeam = async () => {
    if (figmaTeamBusy || !figmaTeamInput.trim() || !active) return
    setFigmaTeamBusy(true)
    try {
      const streams = await setFigmaTeam(active.id, figmaTeamInput.trim())
      toast(`Found ${streams.length} project${streams.length === 1 ? '' : 's'}`, 'success')
      setFigmaTeamInput('')
      await loadSources()
      loadDetails(active.id)
    } catch (e) {
      toast(`Could not read that team: ${e.message}`)
    } finally {
      setFigmaTeamBusy(false)
    }
  }

  const toggleStream = async (stream) => {
    // Optimistic — flip locally, then persist.
    setStreams((list) => list.map((s) => (s.id === stream.id ? { ...s, selected: !s.selected } : s)))
    try {
      const next = await patchSourceStream(active.id, stream.id, { selected: !stream.selected })
      setStreams((list) => list.map((s) => (s.id === next.id ? next : s)))
      await loadSources()
    } catch (e) {
      setStreams((list) => list.map((s) => (s.id === stream.id ? { ...s, selected: stream.selected } : s)))
      toast(`Update failed: ${e.message}`)
    }
  }

  const setAllSelected = async (value) => {
    if (busy) return
    const targets = (streams || []).filter((s) => s.selected !== value)
    if (targets.length === 0) return
    setBusy(true)
    setStreams((list) => list.map((s) => ({ ...s, selected: value })))
    try {
      await Promise.all(targets.map((s) => patchSourceStream(active.id, s.id, { selected: value })))
      await Promise.all([loadDetails(active.id), loadSources()])
    } catch (e) {
      toast(`Bulk update failed: ${e.message}`)
      loadDetails(active.id)
    } finally {
      setBusy(false)
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

  const retry = async (jobId) => {
    if (retryBusy) return
    setRetryBusy(jobId)
    try {
      await retrySourceJob(active.id, jobId)
      toast('Retry queued', 'success')
      await loadDetails(active.id)
    } catch (e) {
      toast(`Retry failed: ${e.message}`)
    } finally {
      setRetryBusy(null)
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

  const addConnectorButton = (variant = 'wb-btn--secondary') => (
    <button className={`wb-btn ${variant} wb-btn--sm`} onClick={() => setPickerOpen(true)}>
      <Plus size={14} strokeWidth={1.8} /> Add connector
    </button>
  )

  const connectedProviders = useMemo(() => new Set(connections.map((c) => c.provider)), [connections])

  // ---- Derived metrics for the active connection ----
  const unit = active ? unitFor(active.provider) : 'streams'
  const jobList = jobs || []
  const importedTotal = jobList.reduce((a, j) => a + (j.stats?.documents || 0), 0)
  const completed = jobList.filter((j) => j.status === 'complete').length
  const failed = jobList.filter((j) => j.status === 'failed').length
  const doneTotal = completed + failed
  const healthPct = doneTotal ? Math.round((completed / doneTotal) * 100) : (jobList.length ? 100 : 0)
  const selectedCount = active?.selected_count ?? selectedStreams.length
  const totalCount = active?.stream_count ?? (streams?.length || 0)
  // Most-recent-last strip of syncs for the "uptime" visual.
  const uptime = useMemo(() => [...jobList].reverse().slice(-26), [jobList])
  const visibleJobs = showAllJobs ? jobList : jobList.slice(0, 4)
  const isSyncing = (active?.active_jobs || 0) > 0
  const manyConns = connections.length > 1

  return (
    <MotionConfig reducedMotion="user">
      <div className="app-page app-page--wide wb-sources">
        <PageHeader
          align="left"
          kicker="Sources"
          title={<>Wire up your <em>sources of truth</em>.</>}
          lede="Connect the tools where decisions actually happen. Pick what to remember — YBase keeps it in sync and cites every answer back to it."
          actions={addConnectorButton()}
        />

        {missingConnectors.length > 0 && (
          <div className="source-alert source-alert--info">
            <Info size={16} strokeWidth={1.8} />
            {missingConnectors.map((p) => INSTALL[p].label).join(', ')} {missingConnectors.length > 1 ? 'need' : 'needs'} setup — add each connector’s <code>*_CLIENT_ID</code> and <code>*_CLIENT_SECRET</code> (plus <code>CONNECTOR_SECRET_KEY</code>) to the backend, then restart to enable {missingConnectors.length > 1 ? 'them' : 'it'}.
          </div>
        )}

        {!sources && (
          <div style={{ marginTop: 'var(--sp-5)', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 72, borderRadius: 'var(--radius-md)' }} />)}
          </div>
        )}

        {sources && connections.length === 0 && (
          <div className="src-section" style={{ marginTop: 'var(--sp-6)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 'var(--sp-4)', textAlign: 'center', padding: 'var(--sp-9)' }}>
            <b style={{ fontSize: 'var(--fs-lg)' }}>No sources connected</b>
            <p className="page-lede" style={{ textAlign: 'center' }}>Connect a system and YBase will remember the decisions inside it.</p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center' }}>{addConnectorButton('wb-btn--primary')}</div>
          </div>
        )}

        {connections.length > 0 && active && (
          <motion.div className="src-stage" key={active.id} variants={staggerContainer(0.06)} initial="hidden" animate="show">
            {/* connection selector — only when there's more than one */}
            {manyConns && (
              <motion.div className="src-selector" variants={fadeUp}>
                {connections.map((c) => (
                  <button key={c.id} className={`src-tab ${active.id === c.id ? 'active' : ''}`} onClick={() => setActiveId(c.id)}>
                    {active.id === c.id && <motion.span layoutId="src-tab-accent" className="src-tab-accent" />}
                    <SrcBadge provider={c.provider} />
                    <span className="src-tab-name">{c.name}</span>
                    <StatusBadge status={c.status} dot />
                  </button>
                ))}
              </motion.div>
            )}

            {/* toolbar: identity + actions */}
            <motion.div className="src-bar" variants={fadeUp}>
              <div className="src-bar-id">
                {!manyConns && <SrcBadge provider={active.provider}>{active.provider}</SrcBadge>}
                <div className="src-bar-name">
                  <h3>{active.name}</h3>
                  <span className="src-extid">{active.external_workspace_id}</span>
                </div>
              </div>
              <div className="src-actions">
                {isSyncing && <span className="src-syncing"><RotateCw size={13} strokeWidth={2} /> syncing</span>}
                <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={() => loadDetails(active.id)}><RotateCw size={14} strokeWidth={1.8} /> Refresh</button>
                <button className="wb-btn wb-btn--primary wb-btn--sm" onClick={runBackfill} disabled={busy || selectedCount === 0}>Backfill 90 days</button>
                <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={disconnect}>Disconnect</button>
              </div>
            </motion.div>

            {active.provider === 'figma' && !active.metadata?.team_id && (
              <motion.div className="source-alert source-alert--info figma-team-prompt" variants={fadeUp}>
                <Info size={16} strokeWidth={1.8} />
                <span>Figma can’t list your teams — paste your team URL or id (from figma.com/files/team/…) to discover its projects.</span>
                <input
                  className="wb-input"
                  placeholder="Team URL or id"
                  value={figmaTeamInput}
                  onChange={(e) => setFigmaTeamInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && saveFigmaTeam()}
                />
                <button className="wb-btn wb-btn--primary wb-btn--sm" onClick={saveFigmaTeam} disabled={figmaTeamBusy || !figmaTeamInput.trim()}>
                  {figmaTeamBusy ? 'Checking…' : 'Save team'}
                </button>
              </motion.div>
            )}

            {active.last_error && <motion.div className="source-alert" variants={fadeUp}><TriangleAlert size={16} strokeWidth={1.8} /> {active.last_error}</motion.div>}

            {active.last_sync_at && active.last_sync_documents === 0 && active.selected_count > 0 && !active.active_jobs && (
              <motion.div className="source-alert source-alert--soft" variants={fadeUp}>
                <Info size={16} strokeWidth={1.8} /> Last sync imported nothing. {EMPTY_IMPORT_HINT[active.provider] || 'There was no new content to import in the sync window.'}
              </motion.div>
            )}

            {/* overview: health ring + facts + sync-history strip */}
            <motion.div className="src-overview" variants={fadeUp}>
              <div className="ov-ring">
                {jobs && doneTotal > 0 ? (
                  <Suspense fallback={<RingFallback />}>
                    <SyncHealthGauge pct={healthPct} accent={tc.accent} trackColor={tc.track} />
                  </Suspense>
                ) : (
                  <div className="ov-ring-empty"><span className="gauge-num">—</span><span className="gauge-lab">no syncs yet</span></div>
                )}
              </div>
              <div className="ov-body">
                <div className="ov-facts">
                  <div className="ov-fact">
                    <span className="ov-num tnum">{streams ? <CountUp value={importedTotal} /> : '—'}</span>
                    <span className="ov-lab">documents imported</span>
                  </div>
                  <div className="ov-fact">
                    <span className="ov-num tnum">{streams ? <CountUp value={selectedCount} /> : '—'}<span className="ov-of"> / {totalCount}</span></span>
                    <span className="ov-lab">{unit} tracked</span>
                  </div>
                  <div className="ov-fact">
                    <span className="ov-num tnum">{jobs ? <CountUp value={jobList.length} /> : '—'}</span>
                    <span className="ov-lab">syncs run</span>
                  </div>
                  {failed > 0 && (
                    <div className="ov-fact">
                      <span className="ov-num tnum" style={{ color: 'var(--danger)' }}><CountUp value={failed} /></span>
                      <span className="ov-lab">failed</span>
                    </div>
                  )}
                </div>
                {uptime.length > 0 && (
                  <div className="uptime">
                    <div className="uptime-head"><span>Sync history</span><span className="uptime-when">last {fmtDate(active.last_sync_at)}</span></div>
                    <div className="uptime-strip">
                      {uptime.map((j) => (
                        <span
                          key={j.id}
                          className={`uptime-seg is-${JOB_TONE[j.status] || 'run'}`}
                          title={`${j.kind?.replace(/_/g, ' ')} · ${j.status} · ${j.stats?.documents || 0} imported · ${fmtDate(j.created_at)}`}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </motion.div>

            {/* two columns: tracked repos | recent activity */}
            <div className="src-cols">
              <motion.div className="src-section" variants={fadeUp}>
                <div className="src-section-head">
                  <h4>Tracked {unit} <span className="count-chip tnum">{selectedCount}</span></h4>
                  <button className={`link-btn ${manageOpen ? 'open' : ''}`} onClick={() => setManageOpen((v) => !v)}>
                    <Settings2 size={15} strokeWidth={1.8} /> Manage all {totalCount} <ChevronDown className="chev" size={15} strokeWidth={1.8} />
                  </button>
                </div>

                {!streams && <div className="wb-skeleton" style={{ height: 44 }} />}
                {streams && (
                  <div className="repo-tracked">
                    {selectedStreams.length === 0 && (
                      <div className="repo-none">No {unit} tracked yet — open “Manage all” to choose what YBase should remember.</div>
                    )}
                    {selectedStreams.map((s) => (
                      <div className="repo-pill" key={s.id}>
                        <span className="repo-name">{active.provider === 'slack' ? `#${s.name}` : s.name}</span>
                        <StatusBadge status={s.status} />
                        <span className="repo-sync">{s.last_synced_at ? `synced ${shortDate(s.last_synced_at)}` : 'not synced'}</span>
                      </div>
                    ))}
                  </div>
                )}

                <AnimatePresence initial={false}>
                  {manageOpen && streams && (
                    <motion.div
                      className="repo-manage"
                      key="manage"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.28, ease: ease.out }}
                    >
                      <div className="repo-manage-inner">
                        <div className="repo-tools">
                          <div className="wb-input-wrap">
                            <span className="wb-input-wrap__affix wb-input-wrap__affix--prefix" aria-hidden="true"><Search size={15} strokeWidth={1.8} /></span>
                            <input className="wb-input wb-input--has-prefix wb-input--sm" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${unit}`} />
                          </div>
                          <div className="repo-bulk">
                            <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => setAllSelected(true)} disabled={busy} title="Select all"><CheckCheck size={14} strokeWidth={1.8} /> All</button>
                            <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => setAllSelected(false)} disabled={busy} title="Clear selection"><Square size={14} strokeWidth={1.8} /> None</button>
                          </div>
                        </div>
                        <div className="repo-scroll">
                          {filteredStreams.length === 0 && <div className="repo-none">No {unit} match “{query}”.</div>}
                          {filteredStreams.map((s) => (
                            <div key={s.id} className={`repo-toggle ${s.selected ? 'on' : ''}`} onClick={() => toggleStream(s)} role="button" tabIndex={0}>
                              <label className="wb-check" onClick={(e) => e.stopPropagation()}>
                                <input type="checkbox" checked={s.selected} onChange={() => toggleStream(s)} />
                                <span className="wb-check__box"><Check size={12} strokeWidth={3} /></span>
                              </label>
                              <span className="repo-t-name">{active.provider === 'slack' ? `#${s.name}` : s.name}</span>
                              <span className="repo-t-meta">
                                {active.provider === 'slack'
                                  ? (s.metadata?.num_members ? `${s.metadata.num_members} members` : '')
                                  : (s.last_synced_at ? `synced ${shortDate(s.last_synced_at)}` : '')}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.div>

              <motion.div className="src-section" variants={fadeUp}>
                <div className="src-section-head">
                  <h4>Recent activity <span className="count-chip tnum">{jobList.length}</span></h4>
                </div>
                {!jobs && <div className="wb-skeleton" style={{ height: 40 }} />}
                {jobs && jobList.length === 0 && <div className="repo-none">No syncs have run yet.</div>}
                <div className="jobs-list">
                  <AnimatePresence initial={false}>
                    {visibleJobs.map((j) => (
                      <motion.div
                        key={j.id}
                        className="job-item"
                        initial={{ opacity: 0, y: -4 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0 }}
                        transition={{ duration: 0.2, ease: ease.out }}
                      >
                        <StatusBadge status={j.status} dot />
                        <div className="job-meta">
                          <div className="job-kind">{j.kind?.replace(/_/g, ' ')}</div>
                          <div className="job-sub">
                            {(j.stats?.documents || 0)} imported · {(j.stats?.duplicates || 0)} skipped
                            {j.next_retry_at ? ` · retry ${fmtDate(j.next_retry_at)}` : ''}
                          </div>
                        </div>
                        <div className="job-right">
                          <span className="job-when">{fmtDate(j.created_at)}</span>
                          {j.status === 'failed' && (
                            <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => retry(j.id)} disabled={retryBusy === j.id}>
                              <RefreshCw size={13} strokeWidth={1.8} /> Retry
                            </button>
                          )}
                        </div>
                        {j.error && <div className="job-err-row">{j.error}</div>}
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </div>
                {jobs && jobList.length > 4 && (
                  <button className={`link-btn jobs-more ${showAllJobs ? 'open' : ''}`} onClick={() => setShowAllJobs((v) => !v)}>
                    {showAllJobs ? 'Show less' : `Show all ${jobList.length}`} <ChevronDown className="chev" size={15} strokeWidth={1.8} />
                  </button>
                )}
              </motion.div>
            </div>
          </motion.div>
        )}
      </div>

      {pickerOpen && (
        <ConnectorPickerModal
          connectedProviders={connectedProviders}
          ready={sources?.configured || {}}
          setupHints={SETUP_HINT}
          onClose={() => setPickerOpen(false)}
          onConnect={(p) => { setPickerOpen(false); connect(p) }}
        />
      )}
    </MotionConfig>
  )
}
