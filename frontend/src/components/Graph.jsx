import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, X } from 'lucide-react'
import { getJSON, getNode } from '../api.js'
import { useToast } from './Toast.jsx'
import { StatusBadge } from '../ybase/ui.jsx'
import PageHeader from '../ybase/PageHeader.jsx'

// Breadth-first 2-hop neighborhood around a node — focus mode shows how one
// memory connects instead of the whole hairball.
function neighborhood(edges, startId, hops = 2) {
  const keep = new Set([startId])
  let frontier = new Set([startId])
  for (let h = 0; h < hops; h++) {
    const next = new Set()
    for (const e of edges) {
      if (frontier.has(e.src) && !keep.has(e.dst)) { keep.add(e.dst); next.add(e.dst) }
      if (frontier.has(e.dst) && !keep.has(e.src)) { keep.add(e.src); next.add(e.src) }
    }
    frontier = next
  }
  return keep
}

const KIND_COLOR = {
  decision: 'var(--accent)',
  question: 'var(--warning)',
  entity: 'var(--text-tertiary)',
  topic: 'var(--src-meeting)',
}
const KIND_R = { decision: 11, question: 9, entity: 7, topic: 7 }
const EDGE_COLOR = {
  revisits: 'var(--danger)',
  resolves: 'var(--success)',
}

const W = 1000
const H = 620

// Small force simulation — repulsion + edge springs + centering gravity.
function simulate(nodes, edges, ticks = 260) {
  const idx = new Map(nodes.map((n, i) => [n.id, i]))
  const pos = nodes.map((_, i) => ({
    x: W / 2 + Math.cos((i / nodes.length) * 2 * Math.PI) * (H / 3),
    y: H / 2 + Math.sin((i / nodes.length) * 2 * Math.PI) * (H / 3),
    vx: 0,
    vy: 0,
  }))
  const springs = edges
    .filter((e) => idx.has(e.src) && idx.has(e.dst))
    .map((e) => [idx.get(e.src), idx.get(e.dst)])

  for (let t = 0; t < ticks; t++) {
    const cool = 1 - t / ticks
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        let dx = pos[i].x - pos[j].x
        let dy = pos[i].y - pos[j].y
        const d2 = dx * dx + dy * dy || 1
        const f = Math.min(9000 / d2, 6) * cool
        const d = Math.sqrt(d2)
        dx /= d; dy /= d
        pos[i].vx += dx * f; pos[i].vy += dy * f
        pos[j].vx -= dx * f; pos[j].vy -= dy * f
      }
    }
    for (const [a, b] of springs) {
      let dx = pos[b].x - pos[a].x
      let dy = pos[b].y - pos[a].y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const f = (d - 120) * 0.04 * cool
      dx /= d; dy /= d
      pos[a].vx += dx * f; pos[a].vy += dy * f
      pos[b].vx -= dx * f; pos[b].vy -= dy * f
    }
    for (const p of pos) {
      p.vx += (W / 2 - p.x) * 0.0025 * cool
      p.vy += (H / 2 - p.y) * 0.004 * cool
      p.x += p.vx * 0.5; p.y += p.vy * 0.5
      p.vx *= 0.82; p.vy *= 0.82
      p.x = Math.max(24, Math.min(W - 24, p.x))
      p.y = Math.max(24, Math.min(H - 24, p.y))
    }
  }
  return nodes.map((n, i) => ({ ...n, x: pos[i].x, y: pos[i].y }))
}

export default function Graph({ focus, onOpenDoc }) {
  const [graph, setGraph] = useState(null)
  const [showEntities, setShowEntities] = useState(true)
  const [showTopics, setShowTopics] = useState(true)
  const [selected, setSelected] = useState(null)
  const [focusId, setFocusId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [drag, setDrag] = useState(null)
  const svgRef = useRef(null)
  const toast = useToast()

  useEffect(() => {
    getJSON('/api/graph').then(setGraph).catch((e) => toast(`Failed to load graph: ${e.message}`))
  }, [toast])

  useEffect(() => {
    if (!focus?.nodeId || !graph) return
    const node = graph.nodes.find((n) => n.id === focus.nodeId)
    if (node) { setSelected(node); setFocusId(node.id) }
  }, [focus, graph])

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    setDetail(null)
    getNode(selected.id).then(setDetail).catch((e) => toast(`Couldn’t load node details: ${e.message}`))
  }, [selected, toast])

  const laid = useMemo(() => {
    if (!graph) return null
    let nodes = graph.nodes.filter(
      (n) => (showEntities || n.kind !== 'entity') && (showTopics || n.kind !== 'topic')
    )
    if (focusId != null) {
      const keep = neighborhood(graph.edges, focusId, 2)
      nodes = graph.nodes.filter((n) => keep.has(n.id))
    }
    const ids = new Set(nodes.map((n) => n.id))
    const edges = graph.edges.filter((e) => ids.has(e.src) && ids.has(e.dst))
    return { nodes: simulate(nodes, edges), edges }
  }, [graph, showEntities, showTopics, focusId])

  const [positions, setPositions] = useState({})
  useEffect(() => { setPositions({}) }, [laid])

  const Header = () => (
    <PageHeader
      kicker="Graph"
      title={<>See how it <em>all connects</em>.</>}
      lede="Decisions, questions, people and topics, linked. Drag nodes to explore; click any one for the detail."
    />
  )

  if (!graph) {
    return <div className="app-page app-page--wide"><Header /><div className="wb-skeleton" style={{ height: 460, marginTop: 'var(--sp-5)', borderRadius: 'var(--radius-lg)' }} /></div>
  }
  if (graph.nodes.length === 0) {
    return (
      <div className="app-page app-page--wide">
        <Header />
        <div className="md-empty" style={{ marginTop: 'var(--sp-6)' }}>Nothing here yet — add documents and the decisions, people, and topics they contain will appear as a connected graph.</div>
      </div>
    )
  }

  const px = (n) => positions[n.id]?.x ?? n.x
  const py = (n) => positions[n.id]?.y ?? n.y
  const byId = new Map(laid.nodes.map((n) => [n.id, n]))

  const toSvgPoint = (e) => {
    const rect = svgRef.current.getBoundingClientRect()
    return { x: ((e.clientX - rect.left) / rect.width) * W, y: ((e.clientY - rect.top) / rect.height) * H }
  }
  const onMove = (e) => {
    if (!drag) return
    const p = toSvgPoint(e)
    setPositions((prev) => ({ ...prev, [drag]: p }))
  }

  const neighbors = selected
    ? new Set(laid.edges.filter((e) => e.src === selected.id || e.dst === selected.id).flatMap((e) => [e.src, e.dst]))
    : null

  return (
    <div className="app-page app-page--wide wb-reveal">
      <Header />

      <div className="graph-controls">
        {focusId != null && (
          <button className="wb-btn wb-btn--ghost wb-btn--sm" onClick={() => setFocusId(null)}>
            <ArrowLeft size={14} strokeWidth={1.8} /> Show whole graph
          </button>
        )}
        <label className="graph-check">
          <input type="checkbox" checked={showEntities} onChange={(e) => setShowEntities(e.target.checked)} />
          People &amp; systems
        </label>
        <label className="graph-check">
          <input type="checkbox" checked={showTopics} onChange={(e) => setShowTopics(e.target.checked)} />
          Topics
        </label>
        <div className="graph-legend">
          <span><i style={{ background: KIND_COLOR.decision }} /> decision</span>
          <span><i style={{ background: KIND_COLOR.question }} /> question</span>
          <span><i style={{ background: KIND_COLOR.entity }} /> entity</span>
          <span><i style={{ background: KIND_COLOR.topic }} /> topic</span>
        </div>
      </div>

      <div className="graph-stage">
        <svg
          ref={svgRef}
          className="graph-svg"
          viewBox={`0 0 ${W} ${H}`}
          onPointerMove={onMove}
          onPointerUp={() => setDrag(null)}
          onPointerLeave={() => setDrag(null)}
        >
          {laid.edges.map((e, i) => {
            const a = byId.get(e.src)
            const b = byId.get(e.dst)
            const dim = neighbors && !(neighbors.has(e.src) && neighbors.has(e.dst))
            return (
              <g key={i} opacity={dim ? 0.15 : 1}>
                <line
                  x1={px(a)} y1={py(a)} x2={px(b)} y2={py(b)}
                  stroke={EDGE_COLOR[e.relation] || 'var(--border-strong)'}
                  strokeWidth={EDGE_COLOR[e.relation] ? 2 : 1.2}
                  strokeDasharray={e.relation === 'revisits' ? '5 4' : 'none'}
                />
                {EDGE_COLOR[e.relation] && (
                  <text x={(px(a) + px(b)) / 2} y={(py(a) + py(b)) / 2 - 4} className="graph-edge-label">{e.relation}</text>
                )}
              </g>
            )
          })}
          {laid.nodes.map((n) => {
            const dim = neighbors && !neighbors.has(n.id) && n.id !== selected?.id
            return (
              <g
                key={n.id}
                opacity={dim ? 0.2 : 1}
                className="graph-node"
                onPointerDown={(e) => { e.preventDefault(); setDrag(n.id) }}
                onClick={() => setSelected(selected?.id === n.id ? null : n)}
              >
                <circle
                  cx={px(n)} cy={py(n)} r={KIND_R[n.kind] || 7}
                  fill={KIND_COLOR[n.kind] || 'var(--text-tertiary)'}
                  stroke={selected?.id === n.id ? 'var(--text)' : 'var(--surface)'}
                  strokeWidth={selected?.id === n.id ? 2.5 : 1.5}
                />
                <text x={px(n)} y={py(n) + (KIND_R[n.kind] || 7) + 14} className="graph-label">
                  {n.label.length > 28 ? n.label.slice(0, 27) + '…' : n.label}
                </text>
              </g>
            )
          })}
        </svg>

        {selected && (
          <aside className="graph-detail">
            <div className="graph-detail-head">
              <span className="dlabel" style={{ color: KIND_COLOR[selected.kind], marginBottom: 0 }}>{selected.kind}</span>
              {selected.status && <StatusBadge status={selected.status} />}
              <button className="graph-close" onClick={() => setSelected(null)} aria-label="Close"><X size={16} strokeWidth={1.8} /></button>
            </div>
            <h4>{selected.label}</h4>
            {selected.summary && <p>{selected.summary}</p>}
            {selected.data?.made_by?.length > 0 && <p className="graph-meta"><span className="dlabel">People</span> {selected.data.made_by.join(', ')}</p>}
            {selected.data?.date && <p className="graph-meta"><span className="dlabel">Date</span> {selected.data.date}</p>}
            {selected.data?.resolution && <p className="graph-meta"><span className="dlabel">Resolution</span> {selected.data.resolution}</p>}
            {focusId !== selected.id && (
              <button className="list-more" onClick={() => setFocusId(selected.id)}>Focus on this node →</button>
            )}
            {detail?.sources?.length > 0 && (
              <>
                <div className="dlabel" style={{ marginTop: 'var(--sp-3)' }}>Evidence</div>
                <div className="dsources">
                  {detail.sources.map((s) => (
                    <button key={s.document_id} className={`src-badge src-${s.source}`} style={{ cursor: 'pointer' }} onClick={() => onOpenDoc && onOpenDoc(s.document_id)}>
                      <i className="src-dot" aria-hidden="true" /> {s.source}: {s.title}
                    </button>
                  ))}
                </div>
              </>
            )}
          </aside>
        )}
      </div>
    </div>
  )
}
