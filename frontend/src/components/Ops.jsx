import React, { useEffect, useMemo, useState } from 'react'
import { RotateCw, CircleCheckBig, TriangleAlert, Check, Minus } from 'lucide-react'
import { getOpsOverview, retryFailedDocuments, retrySourceJob, seedDemoData } from '../api.js'
import { formatDateTime as fmtDate } from '../format.js'
import { useToast } from './Toast.jsx'
import { Badge, StatusBadge, SrcBadge } from '../whybase/ui.jsx'

export default function Ops({ onNavigate, onAsk }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState('')
  const toast = useToast()

  const load = () => getOpsOverview().then(setData).catch((e) => toast(`Failed to load ops dashboard: ${e.message}`))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const shouldPoll = useMemo(() => {
    const c = data?.counts || {}
    return (c.documents_pending || 0) > 0 || (c.documents_processing || 0) > 0 || (c.active_sync_jobs || 0) > 0
  }, [data])

  useEffect(() => {
    if (!shouldPoll) return undefined
    const t = setTimeout(load, 3500)
    return () => clearTimeout(t)
  }, [shouldPoll, data]) // eslint-disable-line react-hooks/exhaustive-deps

  const seedDemo = async () => {
    if (busy) return
    setBusy('seed')
    try {
      const res = await seedDemoData()
      toast(res.created ? `Demo data seeded: ${res.created} documents queued` : `Demo data already present: ${res.duplicates} duplicates skipped`, 'success')
      await load()
    } catch (e) {
      toast(`Demo seed failed: ${e.message}`)
    } finally {
      setBusy('')
    }
  }

  const retryDocs = async () => {
    if (busy) return
    setBusy('docs')
    try {
      const res = await retryFailedDocuments()
      toast(`Requeued ${res.requeued} failed document${res.requeued === 1 ? '' : 's'}`, 'success')
      await load()
    } catch (e) {
      toast(`Retry failed: ${e.message}`)
    } finally {
      setBusy('')
    }
  }

  const retryJob = async (job) => {
    if (busy) return
    setBusy(`job-${job.id}`)
    try {
      await retrySourceJob(job.connection_id, job.id)
      toast('Sync job requeued', 'success')
      await load()
    } catch (e) {
      toast(`Sync retry failed: ${e.message}`)
    } finally {
      setBusy('')
    }
  }

  const counts = data?.counts || {}
  const formationBusy = (counts.documents_pending || 0) + (counts.documents_processing || 0)
  const ready = data?.readiness?.complete

  const metrics = data ? [
    { lab: 'Docs', val: counts.documents || 0, tone: '' },
    { lab: 'Memory nodes', val: counts.memory_nodes || 0, tone: '' },
    { lab: 'Needs review', val: counts.needs_review || 0, tone: counts.needs_review ? 'warn' : '' },
    { lab: 'Open feedback', val: counts.open_feedback || 0, tone: counts.open_feedback ? 'warn' : '' },
    { lab: 'Failed docs', val: counts.documents_failed || 0, tone: counts.documents_failed ? 'bad' : '' },
    { lab: 'Failed syncs', val: counts.failed_sync_jobs || 0, tone: counts.failed_sync_jobs ? 'bad' : '' },
  ] : []

  return (
    <div className="app-page app-page--wide wb-reveal">
      <div className="sources-head">
        <div>
          <div className="eyebrow">Workspace</div>
          <h1 className="page-h1">Ops</h1>
          <p className="page-lede">Readiness, setup guidance and recovery controls for {data?.workspace?.name || 'the workspace'}’s memory pipeline.</p>
        </div>
        <div className="sources-connect" style={{ paddingTop: 26 }}>
          <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={load}><RotateCw size={14} strokeWidth={1.8} /> Refresh</button>
        </div>
      </div>

      {!data && (
        <div style={{ marginTop: 'var(--sp-5)', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[0, 1].map((i) => <div key={i} className="wb-skeleton" style={{ height: 64, borderRadius: 'var(--radius-md)' }} />)}
        </div>
      )}

      {data && (
        <>
          <div className={`ops-banner ${ready ? 'ready' : ''} wb-reveal`} style={{ '--i': 1 }}>
            {ready ? <CircleCheckBig size={22} strokeWidth={1.8} /> : <TriangleAlert size={22} strokeWidth={1.8} style={{ color: 'var(--warning)' }} />}
            <div>
              <b>{ready ? 'Memory pipeline is healthy' : 'Pipeline needs attention'}</b>
              <span>{data.workspace.name} has {(counts.documents || 0).toLocaleString()} documents and {counts.memory_nodes || 0} memory nodes.</span>
            </div>
            <Badge tone={ready ? 'success' : 'warning'} variant="soft" mono dot>{ready ? 'ready' : 'setup'}</Badge>
          </div>

          <div className="ops-metrics">
            {metrics.map((m, i) => (
              <div className={`ops-metric ${m.tone} wb-reveal`} style={{ '--i': i + 2 }} key={m.lab}>
                <span className="ops-metric-val tnum">{m.val}</span>
                <span className="ops-metric-lab">{m.lab}</span>
              </div>
            ))}
          </div>

          <div className="ops-cols">
            <section className="ops-card">
              <h3>Launch checklist</h3>
              <div className="ops-steps">
                {data.readiness.steps.map((step) => (
                  <div className={`ops-step ${step.complete ? 'done' : ''}`} key={step.key}>
                    <span className="ops-check">{step.complete ? <Check size={13} strokeWidth={2.4} /> : <Minus size={13} strokeWidth={2.4} />}</span>
                    <div className="ops-step-main">
                      <b>{step.label}</b>
                      <span>{step.detail}</span>
                    </div>
                    {step.action && <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => onNavigate(step.action)}>Open</button>}
                  </div>
                ))}
              </div>
            </section>

            <section className="ops-card">
              <h3>Provider &amp; queue</h3>
              <div className="ops-kv">
                <div className="kv"><span>LLM</span><b className="tnum">{data.provider.llm_provider}: {data.provider.llm_model}</b></div>
                <div className="kv"><span>Slack OAuth</span><Badge tone={data.provider.slack_configured ? 'success' : 'neutral'} variant="soft" mono>{data.provider.slack_configured ? 'configured' : 'not configured'}</Badge></div>
                <div className="kv"><span>Formation</span><b className="tnum">{formationBusy ? `${counts.documents_processing || 0} processing, ${counts.documents_pending || 0} queued` : 'idle'}</b></div>
                <div className="kv"><span>Workers</span><b className="tnum">{data.formation.workers || 0}</b></div>
              </div>
            </section>
          </div>

          <div className="ops-cols">
            <section className="ops-card">
              <div className="ops-card-head">
                <h3>Formation recovery</h3>
                <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={retryDocs} disabled={busy === 'docs' || !counts.documents_failed}>Retry failed docs</button>
              </div>
              {data.active_documents.length === 0 && data.failed_documents.length === 0 && <div className="md-empty">No active or failed formation jobs.</div>}
              {data.active_documents.map((doc) => (
                <div key={`a-${doc.id}`} className="ops-row">
                  <StatusBadge status={doc.formation_status} dot>{doc.formation_status === 'processing' ? 'forming' : doc.formation_status}</StatusBadge>
                  <b>{doc.title}</b>
                  <SrcBadge provider={doc.source}>{doc.source}</SrcBadge>
                  <span className="ops-row-when tnum">{fmtDate(doc.ingested_at)}</span>
                </div>
              ))}
              {data.failed_documents.map((doc) => (
                <div key={`f-${doc.id}`} className="ops-row bad">
                  <Badge tone="danger" variant="soft" mono dot>failed</Badge>
                  <b>{doc.title}</b>
                  <span className="ops-row-err">{(doc.formation_error || '').split('\n').slice(-1)[0].slice(0, 160)}</span>
                </div>
              ))}
            </section>

            <section className="ops-card">
              <h3>Sources &amp; syncs</h3>
              {data.sources.length === 0 && (
                <div className="md-empty">No connected sources yet. <button className="list-more" style={{ display: 'inline', padding: 0 }} onClick={() => onNavigate('sources')}>Open Sources →</button></div>
              )}
              {data.sources.map((source) => (
                <div key={source.id} className={`ops-row ${source.last_error ? 'bad' : ''}`}>
                  <SrcBadge provider={source.provider}>{source.provider}</SrcBadge>
                  <b>{source.name}</b>
                  <span className="ops-row-when tnum">{source.selected_count || 0}/{source.stream_count || 0} · {fmtDate(source.last_sync_at)}</span>
                  {source.last_error && <span className="ops-row-err">{source.last_error}</span>}
                </div>
              ))}
              {data.sync_jobs.map((job) => (
                <div key={job.id} className={`ops-row ${job.status === 'failed' ? 'bad' : ''}`}>
                  <StatusBadge status={job.status}>{job.status.replace('_', ' ')}</StatusBadge>
                  <b>{job.connection_name} {job.kind}</b>
                  <span className="ops-row-when tnum">{job.stats?.documents || 0} docs · {fmtDate(job.updated_at)}</span>
                  {job.error && <span className="ops-row-err">{job.error}</span>}
                  {(job.status === 'failed' || job.status === 'paused') && (
                    <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => retryJob(job)} disabled={busy === `job-${job.id}`}>Retry sync</button>
                  )}
                </div>
              ))}
            </section>
          </div>

          <section className="ops-card">
            <div className="ops-card-head">
              <h3>Demo dataset</h3>
              <button className="wb-btn wb-btn--secondary wb-btn--sm" onClick={seedDemo} disabled={busy === 'seed'}>Seed demo data</button>
            </div>
            <p className="ops-copy">Adds four sample company-history documents through the normal ingest pipeline, then memory formation extracts decisions, questions, people, topics and provenance.</p>
            <div className="starters">
              {data.demo_questions.map((q) => <button key={q} className="chip-q" onClick={() => onAsk(q)}>{q}</button>)}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
