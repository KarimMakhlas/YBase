import React, { useEffect, useRef, useState } from 'react'
import {
  motion, MotionConfig, AnimatePresence, useScroll, useMotionValueEvent, useReducedMotion,
} from 'framer-motion'
import {
  Sparkles, ArrowRight, Search, Menu, X, Check, Quote, RotateCcw, GitCommitHorizontal,
  Users, ShieldCheck, Scale, MessageSquare, SquareCheck, CircleHelp, FileText, User,
  Download, GitFork, MessageSquareQuote,
} from 'lucide-react'
import ybaseMark from '../assets/ybase-mark.svg'
import { staggerContainer, fadeUp, spring, ease, inView } from '../ybase/motionPresets'
import '../ybase/marketing.css'

// ---- Content -----------------------------------------------------------

const SRC = {
  Slack: 'var(--mk-slack)', Notion: 'var(--mk-notion)', GitHub: 'var(--mk-github)',
  Jira: 'var(--mk-jira)', Meeting: 'var(--mk-meeting)',
}

// Answers are segment lists: text runs (optionally bold) and citation chips.
// tokenize() splits the text into word tokens so the demo can stream them.
const tokenize = (segs) => segs.flatMap((s) => (
  s.cite ? [s] : s.t.split(/(?<= )/).map((t) => ({ t, b: s.b }))
))

const DEMOS = [
  {
    q: 'Why did we choose Postgres over MongoDB?',
    conf: 'high',
    srcs: ['Slack', 'Notion', 'Jira'],
    a: [
      { t: 'The team chose ' }, { t: 'PostgreSQL', b: true },
      { t: ' as the single datastore for v1 — for transactional integrity in billing' }, { cite: 'C1' },
      { t: ', operational experience over running a Mongo cluster' }, { cite: 'C2' },
      { t: ', and because JSONB + pgvector keep the stack to one database.' }, { cite: 'C3' },
      { t: ' Dev Patel pushed for Mongo on the activity feed; revisited in Jan 2026 and ' },
      { t: 'reaffirmed', b: true }, { t: '.' }, { cite: 'C4' },
    ],
  },
  {
    q: 'What did we decide about usage-based pricing?',
    conf: 'medium',
    srcs: ['Slack', 'Meeting', 'Notion'],
    a: [
      { t: 'Dropped for launch — v1 ships ' }, { t: 'flat per-seat pricing', b: true }, { t: '.' }, { cite: 'C1' },
      { t: ' Maya Chen argued metering required a billing rearchitecture' }, { cite: 'C2' },
      { t: ', and sales flagged deal friction in three pilots' }, { cite: 'C3' },
      { t: '. A revisit is on the record for when ingestion costs stabilize.' }, { cite: 'C4' },
    ],
  },
].map((d) => ({ ...d, words: tokenize(d.a) }))

const STEPS = [
  { Icon: Download, n: '01', title: 'Ingest & dedup', body: 'Connect Slack, Notion, GitHub and Jira. Threads become documents; near-duplicates merge by content hash before anything is stored.' },
  { Icon: GitFork, n: '02', title: 'Form memory', body: 'An LLM extracts decisions, reasoning, advocates and alternatives — then links them into a typed graph: revisits, resolves, involves, about.' },
  { Icon: MessageSquareQuote, n: '03', title: 'Answer with provenance', body: 'Hybrid retrieval plus 2-hop graph expansion finds evidence others miss — and every claim is cited back to the source that settled it.' },
]

const CELLS = [
  { Icon: GitCommitHorizontal, title: 'Decision log', body: 'Positions on the record, alternatives considered, and the chain of revisits — for every decision your team has made.' },
  { Icon: Users, title: 'People pages', body: 'Everything someone advocated, decided or raised — with their recorded positions quoted verbatim.' },
  { Icon: ShieldCheck, title: 'Runs fully local', body: 'Ollama for LLM and embeddings, zero API keys — or Claude + Voyage when credentials are present.' },
  { Icon: Scale, title: 'Honest confidence', body: 'Answers score their own confidence and surface counter-evidence. A structured “not in memory” beats a confident guess.' },
]

const G_NODES = [
  { id: 'dec', x: 50, y: 40, Icon: GitCommitHorizontal, label: 'Postgres for v1', lead: true, color: 'var(--mk-violet)' },
  { id: 'debate', x: 18, y: 12, Icon: MessageSquare, label: '#engineering debate', color: 'var(--mk-slack)' },
  { id: 'jira', x: 79, y: 15, Icon: SquareCheck, label: 'ENG-481 near-reversal', color: 'var(--mk-jira)' },
  { id: 'rfc', x: 16, y: 67, Icon: FileText, label: 'RFC: datastore for v1', color: 'var(--mk-notion)' },
  { id: 'dev', x: 83, y: 64, Icon: User, label: 'Dev Patel', color: 'var(--mk-green)' },
  { id: 'open', x: 50, y: 88, Icon: CircleHelp, label: 'Read replicas before launch?', color: 'var(--mk-amber)' },
]
const G_EDGES = [
  ['debate', 'dec', 'revisits'], ['jira', 'dec', 'revisits'], ['rfc', 'dec', 'resolves'],
  ['dev', 'dec', 'involves'], ['dec', 'open', 'opens'],
]

const TIERS = [
  {
    name: 'Self-host', price: '$0', per: 'forever',
    blurb: 'Run YBase on your own hardware. Fully local, your data never leaves.',
    items: ['All features, unlimited seats', 'Ollama pipeline — zero API keys', 'Community support'],
    cta: 'Start free',
  },
  {
    name: 'Team', badge: 'Early access', price: '$12', per: 'per user / month', hot: true,
    blurb: 'Hosted YBase with the Claude-powered memory pipeline.',
    items: ['Claude + Voyage memory formation', 'Slack, Notion, GitHub & Jira sync', 'Priority support'],
    cta: 'Start free',
  },
  {
    name: 'Enterprise', price: 'Custom', per: 'annual',
    blurb: 'For teams with compliance and deployment requirements.',
    items: ['SSO / SAML', 'VPC or on-prem deployment', 'Audit log & retention controls'],
    cta: 'Talk to us', href: 'mailto:makhlasabdelkarim@gmail.com',
  },
]

const LINKS = [
  ['how', 'How it works'], ['features', 'Features'], ['graph', 'Memory graph'], ['pricing', 'Pricing'],
]

// ---- Hero constellation ----------------------------------------------
// A faint node/edge field behind the headline — the memory graph as texture.
// Two layers drift at different speeds; two pulses travel along fixed edges.

const C_NODES = [
  [70, 90], [200, 42], [330, 120], [470, 36], [600, 100], [730, 48], [870, 130], [1000, 60], [1130, 110],
  [110, 240], [260, 205], [410, 255], [560, 215], [710, 265], [860, 210], [1010, 250], [1140, 215],
  [180, 380], [350, 345], [520, 400], [690, 355], [850, 395], [1020, 350],
]
const C_EDGES = [
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8],
  [1, 10], [2, 11], [4, 12], [5, 13], [7, 15], [8, 16],
  [9, 10], [10, 11], [11, 12], [12, 13], [13, 14], [14, 15], [15, 16],
  [9, 17], [11, 18], [13, 19], [14, 20], [16, 22],
  [17, 18], [18, 19], [19, 20], [20, 21], [21, 22],
]
const C_DUST = [[140, 155], [420, 165], [640, 320], [90, 320], [960, 160], [1180, 300], [760, 145], [300, 305]]

function Constellation({ reduce }) {
  return (
    <div className="mk-stars" aria-hidden="true">
      <svg viewBox="0 0 1200 460" preserveAspectRatio="xMidYMin slice">
        <g className="drift-a">
          {C_EDGES.map(([a, b], i) => (
            <line
              key={i}
              x1={C_NODES[a][0]} y1={C_NODES[a][1]} x2={C_NODES[b][0]} y2={C_NODES[b][1]}
              stroke="rgba(157, 140, 255, 0.13)" strokeWidth="1"
            />
          ))}
          {C_NODES.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={i % 5 === 0 ? 3 : 2.2} fill={`rgba(157, 140, 255, ${i % 3 === 0 ? 0.55 : 0.32})`} />
          ))}
          {!reduce && (
            <>
              <circle r="3" fill="#9d8cff" opacity="0.9">
                <animateMotion dur="4.2s" repeatCount="indefinite" path="M600,100 L560,215" />
              </circle>
              <circle r="3" fill="#9d8cff" opacity="0.9">
                <animateMotion dur="5.6s" begin="2s" repeatCount="indefinite" path="M710,265 L520,400" />
              </circle>
            </>
          )}
        </g>
        <g className="drift-b">
          {C_DUST.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r="1.6" fill="rgba(111, 156, 245, 0.35)" />
          ))}
        </g>
      </svg>
    </div>
  )
}

// ---- Streaming answer demo --------------------------------------------
// The page's one showpiece: the question types itself, the answer streams
// word-by-word with citation chips popping in, then the meta row lands.
// Loops through DEMOS. When `live` is false (reduced motion or a narrow
// viewport) it renders the first demo fully formed instead.

function AskDemo({ live }) {
  const [di, setDi] = useState(0)
  const [phase, setPhase] = useState('typing') // typing → think → stream → done → swap
  const [qn, setQn] = useState(0)
  const [an, setAn] = useState(0)
  const demo = DEMOS[live ? di : 0]

  useEffect(() => {
    if (!live) return undefined
    let t
    if (phase === 'typing') {
      t = qn < demo.q.length
        ? setTimeout(() => setQn((n) => n + 1), 26)
        : setTimeout(() => setPhase('think'), 360)
    } else if (phase === 'think') {
      t = setTimeout(() => setPhase('stream'), 950)
    } else if (phase === 'stream') {
      t = an < demo.words.length
        ? setTimeout(() => setAn((n) => n + 1), 30)
        : setTimeout(() => setPhase('done'), 200)
    } else if (phase === 'done') {
      t = setTimeout(() => setPhase('swap'), 4400)
    } else if (phase === 'swap') {
      t = setTimeout(() => {
        setDi((i) => (i + 1) % DEMOS.length)
        setQn(0); setAn(0); setPhase('typing')
      }, 420)
    }
    return () => clearTimeout(t)
  }, [live, phase, qn, an, di, demo])

  const words = live ? demo.words.slice(0, an) : demo.words
  const answering = !live || phase === 'stream' || phase === 'done' || phase === 'swap'
  const metaShown = !live || phase === 'done' || phase === 'swap'

  return (
    <motion.div className="mk-card mk-demo" variants={fadeUp}>
      <div className="mk-demo-top">
        <span>ybase — ask memory</span>
        <span className="mk-live"><i />live on your sources</span>
      </div>
      <div className="mk-demo-body">
        <div className={`mk-demo-swap${live && phase === 'swap' ? ' out' : ''}`}>
          <div className="mk-q">
            {live ? demo.q.slice(0, qn) : demo.q}
            {live && phase === 'typing' && <span className="mk-caret" />}
          </div>
          {live && phase === 'think' && <span className="mk-think"><i /><i /><i /></span>}
          {answering && (
            <p className="mk-ans">
              {words.map((w, i) => (w.cite
                ? <span className="mk-cite" key={i}>{w.cite}</span>
                : <span className={w.b ? 'b' : undefined} key={i}>{w.t}</span>
              ))}
            </p>
          )}
          <div className={`mk-meta${metaShown ? ' show' : ''}`}>
            <span className={`mk-conf${demo.conf === 'medium' ? ' med' : ''}`}>confidence: {demo.conf}</span>
            {demo.srcs.map((s) => (
              <span className="mk-src" key={s}><i style={{ background: SRC[s] }} />{s}</span>
            ))}
          </div>
        </div>
      </div>
    </motion.div>
  )
}

// ---- Interactive memory graph -------------------------------------------
// Hand-placed nodes over an SVG edge layer. Hovering a node lights up its
// edges and neighbours and dims the rest.

const popV = {
  hidden: { opacity: 0, scale: 0.85 },
  show: (i) => ({ opacity: 1, scale: 1, transition: { ...spring.soft, delay: i * 0.09 } }),
}
const edgeV = {
  hidden: { pathLength: 0, opacity: 0 },
  show: (i) => ({ pathLength: 1, opacity: 1, transition: { duration: 0.5, ease: ease.out, delay: 0.35 + i * 0.09 } }),
}

function MemoryGraph({ viewport }) {
  const [hover, setHover] = useState(null)
  const pos = Object.fromEntries(G_NODES.map((n) => [n.id, n]))
  const touches = (a, b) => hover === a || hover === b
  const neighbour = (id) => G_EDGES.some(([a, b]) => (a === hover && b === id) || (b === hover && a === id))

  return (
    <motion.div
      className="mk-stage"
      initial="hidden" whileInView="show" viewport={viewport}
      onMouseLeave={() => setHover(null)}
    >
      <svg className="mk-gsvg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {G_EDGES.map(([a, b], i) => (
          <motion.line
            key={`${a}-${b}`}
            x1={pos[a].x} y1={pos[a].y} x2={pos[b].x} y2={pos[b].y}
            vectorEffect="non-scaling-stroke"
            className={hover ? (touches(a, b) ? 'hot' : 'dim') : undefined}
            variants={edgeV} custom={i}
          />
        ))}
      </svg>
      {G_EDGES.map(([a, b, label]) => (
        <span
          key={label + a}
          className={`mk-elabel${hover ? (touches(a, b) ? ' hot' : ' dim') : ''}`}
          style={{ left: `${(pos[a].x + pos[b].x) / 2}%`, top: `${(pos[a].y + pos[b].y) / 2}%` }}
        >
          {label}
        </span>
      ))}
      {G_NODES.map((n, i) => (
        <motion.div
          key={n.id}
          className={`mk-gnode${n.lead ? ' lead' : ''}${hover === n.id ? ' hot' : ''}${hover && hover !== n.id && !neighbour(n.id) ? ' dim' : ''}`}
          style={{ left: `${n.x}%`, top: `${n.y}%`, x: '-50%', y: '-50%' }}
          variants={popV} custom={i}
          onMouseEnter={() => setHover(n.id)}
        >
          <n.Icon size={16} strokeWidth={1.8} style={{ color: n.color }} />
          {n.label}
        </motion.div>
      ))}
    </motion.div>
  )
}

// ---- Page ----------------------------------------------------------------
// The YBase marketing landing page. Shown to logged-out visitors; CTAs lead
// into the product (sign in or start a workspace). Dark-first — the page
// carries its own theme regardless of the app's.

export default function Marketing({ onEnter }) {
  const rootRef = useRef(null)
  const reduce = useReducedMotion()
  const [scrolled, setScrolled] = useState(false)
  const [active, setActive] = useState(null)
  const [menu, setMenu] = useState(false)
  // Decided once at mount: on narrow viewports the demo renders fully formed
  // and the constellation is hidden — no animation budget spent on mobile.
  const [narrow] = useState(() => window.matchMedia('(max-width: 860px)').matches)

  // The page scrolls inside .mkt (not the window), so the scroll listener,
  // whileInView observers and the section spy all bind to that container.
  const { scrollY } = useScroll({ container: rootRef })
  useMotionValueEvent(scrollY, 'change', (v) => {
    setScrolled(v > 8)
    // Back near the top there is no current section — drop the nav highlight.
    if (v < 200) setActive(null)
  })
  const viewport = { ...inView, root: rootRef }

  useEffect(() => {
    const root = rootRef.current
    if (!root) return undefined
    const spy = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) setActive(e.target.id) })
    }, { root, rootMargin: '-35% 0px -60% 0px' })
    LINKS.forEach(([id]) => {
      const el = root.querySelector(`#${id}`)
      if (el) spy.observe(el)
    })
    return () => spy.disconnect()
  }, [])

  const enter = (intent) => (e) => { e.preventDefault(); onEnter(intent) }
  const goTo = (id) => (e) => {
    e.preventDefault()
    setMenu(false)
    rootRef.current?.querySelector(`#${id}`)?.scrollIntoView({ behavior: 'smooth' })
  }

  return (
    <MotionConfig reducedMotion="user">
      <div className="mkt" ref={rootRef}>
        <nav className={`mk-nav${scrolled ? ' scrolled' : ''}`}>
          <div className="mk-nav-in">
            <a className="mk-brand" href="#top" onClick={(e) => { e.preventDefault(); rootRef.current?.scrollTo({ top: 0, behavior: 'smooth' }) }}>
              <img src={ybaseMark} alt="" />YBase
            </a>
            <div className="mk-links">
              {LINKS.map(([id, label]) => (
                <a key={id} href={`#${id}`} onClick={goTo(id)} className={active === id ? 'on' : undefined}>{label}</a>
              ))}
            </div>
            <div className="mk-navcta">
              <a className="mk-btn mk-btn--ghost" href="#/login" onClick={enter('login')}>Sign in</a>
              <a className="mk-btn mk-btn--pri" href="#/signup" onClick={enter('signup')}>Start free</a>
            </div>
            <button className="mk-burger" onClick={() => setMenu(true)} aria-label="Open menu"><Menu size={22} /></button>
          </div>
        </nav>

        <AnimatePresence>
          {menu && (
            <motion.div
              className="mk-menu"
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              transition={{ duration: 0.18 }}
            >
              <div className="mk-menu-top">
                <span className="mk-brand"><img src={ybaseMark} alt="" />YBase</span>
                <button className="mk-burger" onClick={() => setMenu(false)} aria-label="Close menu"><X size={22} /></button>
              </div>
              <nav>
                {LINKS.map(([id, label]) => <a key={id} href={`#${id}`} onClick={goTo(id)}>{label}</a>)}
              </nav>
              <div className="mk-menu-cta">
                <a className="mk-btn mk-btn--pri mk-btn--lg" href="#/signup" onClick={enter('signup')}>Start free</a>
                <a className="mk-btn mk-btn--ghost mk-btn--lg" href="#/login" onClick={enter('login')}>Sign in</a>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <section className="mk-hero">
          <Constellation reduce={reduce || narrow} />
          <div className="mk-hero-glow" aria-hidden="true" />
          <motion.div
            className="mk-wrap"
            variants={staggerContainer(0.09, 0.05)}
            initial="hidden" animate="show"
          >
            <motion.a className="mk-pill" href="#how" onClick={goTo('how')} variants={fadeUp}>
              <span className="k"><Sparkles size={13} strokeWidth={1.8} /> New</span>
              <b>Claude-powered</b> memory formation <ArrowRight size={14} strokeWidth={1.8} />
            </motion.a>
            <motion.h1 className="mk-h1" variants={fadeUp}>
              Never lose the <em>why</em> behind your team&apos;s decisions
            </motion.h1>
            <motion.p className="mk-sub" variants={fadeUp}>
              An AI memory layer over Slack, Notion, GitHub and Jira. YBase remembers what your
              team decided — and why — and answers with the receipts.
            </motion.p>
            <motion.div className="mk-ctas" variants={fadeUp}>
              <a className="mk-btn mk-btn--pri mk-btn--lg" href="#/signup" onClick={enter('signup')}>
                Start free <ArrowRight size={16} strokeWidth={2} />
              </a>
              <a className="mk-btn mk-btn--ghost mk-btn--lg" href="#how" onClick={goTo('how')}>See how it works</a>
            </motion.div>

            <AskDemo live={!reduce && !narrow} />

            <motion.div className="mk-stats" variants={fadeUp}>
              <span>4 sources, one memory</span>
              <span>Every claim cited</span>
              <span>Runs fully local</span>
              <span>Zero API keys required</span>
            </motion.div>
          </motion.div>
        </section>

        <section className="mk-sec mk-sec--band" id="compare">
          <div className="mk-wrap">
            <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Search vs. memory</motion.span>
            <motion.h2 className="mk-h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Search finds documents. Memory answers questions.</motion.h2>
            <motion.p className="mk-lede" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              Ask search <em>why</em> and you get forty documents that mention the debate. Ask memory
              and you get the decision, who made it, and the evidence — in one answer.
            </motion.p>
            <motion.div className="mk-cmp" variants={staggerContainer(0.14)} initial="hidden" whileInView="show" viewport={viewport}>
              <motion.div className="mk-card mk-cmp-card dud" variants={fadeUp}>
                <div className="mk-cmp-tag">Workspace search</div>
                <div className="mk-srch-row"><Search strokeWidth={1.8} />postgres vs mongodb decision</div>
                <div className="mk-res">Datastore RFC (v3)<small>Notion · edited 14 months ago — outdated</small></div>
                <div className="mk-res">#engineering<small>Slack · 214 messages, 3 threads</small></div>
                <div className="mk-res">postgres-vs-mongo-benchmarks.md<small>GitHub · archived repo</small></div>
                <div className="mk-res">ENG-481: Evaluate MongoDB for activity feed<small>Jira · closed, no resolution note</small></div>
                <div className="mk-res">Re: re: datastore decision??<small>Slack · thread, 41 replies</small></div>
                <div className="mk-cmp-foot">47 results · 0 answers</div>
              </motion.div>
              <motion.div className="mk-card mk-cmp-card" variants={fadeUp}>
                <div className="mk-cmp-tag">YBase — ask memory</div>
                <div className="mk-cmp-q">Why did we choose Postgres over MongoDB?</div>
                <div className="mk-cmp-ans">
                  <span className="b">Postgres, decided Sep 2025</span> — transactional integrity in
                  billing<span className="mk-cite">C1</span>, ops experience<span className="mk-cite">C2</span>,
                  and JSONB + pgvector keep the stack to one database<span className="mk-cite">C3</span>.
                  Challenged in Jan 2026 (ENG-481) and <span className="b">reaffirmed</span>.<span className="mk-cite">C4</span>
                </div>
                <div className="mk-cmp-foot good">1 answer · 4 citations · reaffirmed Jan 2026</div>
              </motion.div>
            </motion.div>
          </div>
        </section>

        <section className="mk-sec" id="how">
          <div className="mk-wrap">
            <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>How it works</motion.span>
            <motion.h2 className="mk-h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Memory, not search</motion.h2>
            <motion.p className="mk-lede" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              Every document your team writes flows through the same pipeline — ingested, understood,
              and connected into a graph you can ask questions of.
            </motion.p>
            <motion.div className="mk-steps" variants={staggerContainer(0.12)} initial="hidden" whileInView="show" viewport={viewport}>
              <motion.div
                className="mk-pipe" aria-hidden="true"
                variants={{ hidden: { scaleX: 0, opacity: 0 }, show: { scaleX: 1, opacity: 1, transition: { duration: 1.1, ease: ease.inOut } } }}
              >
                {!reduce && <><i /><i /></>}
              </motion.div>
              {STEPS.map((s) => (
                <motion.div className="mk-card mk-step" variants={fadeUp} key={s.n}>
                  <div className="mk-step-icon"><s.Icon size={20} strokeWidth={1.8} /></div>
                  <div className="mk-step-n">{s.n}</div>
                  <h3>{s.title}</h3>
                  <p>{s.body}</p>
                </motion.div>
              ))}
            </motion.div>
          </div>
        </section>

        <section className="mk-sec mk-sec--band" id="features">
          <div className="mk-wrap">
            <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Why teams trust it</motion.span>
            <motion.h2 className="mk-h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Answers you can defend</motion.h2>
            <motion.div className="mk-bento" variants={staggerContainer(0.08)} initial="hidden" whileInView="show" viewport={viewport}>
              <motion.div className="mk-card mk-cell lg" variants={fadeUp}>
                <div className="mk-cell-icon"><Quote size={18} strokeWidth={1.8} /></div>
                <h3>Full provenance</h3>
                <p>Every answer carries citation chips that open the original Slack message or doc, with the cited span highlighted.</p>
                <div className="mk-prov">
                  <div className="mk-prov-claim">…for transactional integrity in billing<span className="mk-cite">C1</span></div>
                  <div className="mk-prov-pop">
                    <small><i />Slack · #engineering · Sep 14</small>
                    <q>we can&apos;t risk billing consistency on eventual consistency — <mark>billing needs real transactions</mark></q>
                  </div>
                </div>
              </motion.div>
              <motion.div className="mk-card mk-cell lg" variants={fadeUp}>
                <div className="mk-cell-icon"><RotateCcw size={18} strokeWidth={1.8} /></div>
                <h3>Revisit detection</h3>
                <p>When a new document relitigates a settled decision, YBase surfaces it — so you never re-argue what you already decided.</p>
                <div className="mk-rev">
                  <div className="mk-rev-node">Sep 2025<b>Postgres for v1</b></div>
                  <div className="mk-rev-line" aria-hidden="true">
                    <svg preserveAspectRatio="none"><line x1="0" y1="12" x2="100%" y2="12" /></svg>
                    <em>revisits</em>
                  </div>
                  <div className="mk-rev-node">Jan 2026<b>ENG-481 reopens it</b></div>
                </div>
              </motion.div>
              {CELLS.map((f) => (
                <motion.div className="mk-card mk-cell" variants={fadeUp} key={f.title}>
                  <div className="mk-cell-icon"><f.Icon size={18} strokeWidth={1.8} /></div>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </motion.div>
              ))}
            </motion.div>
          </div>
        </section>

        <section className="mk-sec" id="graph">
          <div className="mk-wrap mk-split">
            <div>
              <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>The memory graph</motion.span>
              <motion.h2 className="mk-h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>It connects what search can&apos;t</motion.h2>
              <motion.p className="mk-lede" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
                A question that vector-matches the September Slack debate also surfaces the January
                Jira near-reversal — because the graph edge <code>revisits</code> connects them, even
                when the ticket shares few words with the question.
              </motion.p>
              <motion.div variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport} style={{ marginTop: 28 }}>
                <a className="mk-btn mk-btn--pri mk-btn--lg" href="#/signup" onClick={enter('signup')}>
                  Explore the graph <ArrowRight size={16} strokeWidth={2} />
                </a>
                <div className="mk-graph-hint">hover a node — its edges light up</div>
              </motion.div>
            </div>
            <MemoryGraph viewport={viewport} />
          </div>
        </section>

        <section className="mk-sec mk-sec--band">
          <div className="mk-wrap" style={{ textAlign: 'center' }}>
            <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Sources</motion.span>
            <motion.h2 className="mk-h2" style={{ marginInline: 'auto' }} variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Plugs into where decisions happen</motion.h2>
            <motion.div className="mk-int" variants={staggerContainer(0.06)} initial="hidden" whileInView="show" viewport={viewport}>
              {['Slack', 'Notion', 'GitHub', 'Jira', 'Meeting'].map((s) => (
                <motion.span className="mk-int-chip" variants={fadeUp} key={s}>
                  <i style={{ background: SRC[s] }} />{s === 'Meeting' ? 'Meeting notes' : s}
                </motion.span>
              ))}
            </motion.div>
            <motion.p className="mk-int-note" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              Runs fully local with Ollama, or Claude + Voyage when credentials are present.
              The switch is one <code>env</code> var.
            </motion.p>
          </div>
        </section>

        <section className="mk-sec" id="pricing">
          <div className="mk-wrap">
            <motion.span className="mk-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Pricing</motion.span>
            <motion.h2 className="mk-h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Self-host for free. Pay when we host.</motion.h2>
            <motion.div className="mk-tiers" variants={staggerContainer(0.12)} initial="hidden" whileInView="show" viewport={viewport}>
              {TIERS.map((t) => (
                <motion.div className={`mk-card mk-tier${t.hot ? ' hot' : ''}`} variants={fadeUp} key={t.name}>
                  <div className="mk-tier-head">
                    <h3>{t.name}</h3>
                    {t.badge && <span className="mk-badge">{t.badge}</span>}
                  </div>
                  <div className="mk-price">{t.price}<small>{t.per}</small></div>
                  <p className="mk-tier-blurb">{t.blurb}</p>
                  <ul>
                    {t.items.map((it) => <li key={it}><Check strokeWidth={2.2} />{it}</li>)}
                  </ul>
                  {t.href
                    ? <a className="mk-btn mk-btn--ghost" href={t.href}>{t.cta}</a>
                    : <a className={`mk-btn ${t.hot ? 'mk-btn--pri' : 'mk-btn--ghost'}`} href="#/signup" onClick={enter('signup')}>{t.cta}</a>}
                </motion.div>
              ))}
            </motion.div>
          </div>
        </section>

        <section className="mk-sec mk-fin">
          <div className="mk-wrap">
            <motion.h2 variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Stop re-arguing settled decisions</motion.h2>
            <motion.p variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              Give your team a memory that keeps the reasoning, not just the outcome.
              Set up in minutes — fully local, or Claude-powered.
            </motion.p>
            <motion.div className="mk-ctas" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              <a className="mk-btn mk-btn--pri mk-btn--lg" href="#/signup" onClick={enter('signup')}>
                Start free <ArrowRight size={16} strokeWidth={2} />
              </a>
              <a className="mk-btn mk-btn--ghost mk-btn--lg" href="#/login" onClick={enter('login')}>Sign in</a>
            </motion.div>
          </div>
        </section>

        <footer className="mk-foot">
          <div className="mk-wrap mk-foot-in">
            <div className="mk-foot-brand">
              <span className="mk-brand"><img src={ybaseMark} alt="" />YBase</span>
              <p>An AI memory layer for engineering teams. Never lose the why behind your decisions.</p>
            </div>
            <div className="mk-foot-cols">
              <div className="mk-foot-col">
                <h4>Product</h4>
                {LINKS.map(([id, label]) => <a key={id} href={`#${id}`} onClick={goTo(id)}>{label}</a>)}
              </div>
              <div className="mk-foot-col">
                <h4>Get started</h4>
                <a href="#/signup" onClick={enter('signup')}>Start free</a>
                <a href="#/login" onClick={enter('login')}>Sign in</a>
              </div>
            </div>
          </div>
          <div className="mk-foot-base">
            <span>© 2026 YBase</span>
            <span>runs fully local · no API keys required</span>
          </div>
        </footer>
      </div>
    </MotionConfig>
  )
}
