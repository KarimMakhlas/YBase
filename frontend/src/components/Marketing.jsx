import React, { useRef, useState } from 'react'
import {
  motion, MotionConfig, useMotionValue, useSpring, useTransform,
  useScroll, useMotionValueEvent, useReducedMotion,
} from 'framer-motion'
import {
  Sparkles, ArrowRight, Download, GitFork, MessageSquareQuote, Quote, RotateCcw,
  GitCommitHorizontal, Users, ShieldCheck, Scale, MessageSquare, SquareCheck, CircleHelp,
} from 'lucide-react'
import ybaseMark from '../assets/ybase-mark.svg'
import { staggerContainer, fadeUp, spring, ease, inView } from '../ybase/motionPresets'
import '../ybase/marketing.css'

// ---- Graph orchestration variants ----------------------------------------
// `custom` carries the row index so nodes and the line beneath them animate as
// a downward "build": node 0 pops, its connector draws, node 1 pops, etc.
const nodeV = {
  hidden: { opacity: 0, scale: 0.92, y: 12 },
  show: (i) => ({ opacity: 1, scale: 1, y: 0, transition: { ...spring.soft, delay: i * 0.28 } }),
}
const lineV = {
  hidden: { pathLength: 0, opacity: 0 },
  show: (i) => ({ pathLength: 1, opacity: 1, transition: { duration: 0.34, ease: ease.out, delay: i * 0.28 + 0.18 } }),
}

// A connector between two graph nodes: a short vertical line that draws itself,
// with an optional pulse travelling down it to suggest a live "revisit" edge.
function GConn({ index, pulse, reduce }) {
  return (
    <div className="gconn">
      <svg className="gconn-svg" width="3" height="20" viewBox="0 0 3 20" preserveAspectRatio="none" aria-hidden="true">
        <motion.line
          x1="1.5" y1="0" x2="1.5" y2="20"
          stroke="var(--border-strong)" strokeWidth="2" strokeLinecap="round"
          variants={lineV} custom={index}
        />
      </svg>
      {pulse && !reduce && (
        <motion.span
          className="gpulse"
          aria-hidden="true"
          animate={{ top: ['-3px', '20px'], opacity: [0, 1, 0] }}
          transition={{ duration: 1.8, repeat: Infinity, repeatDelay: 1.1, ease: 'easeInOut' }}
        />
      )}
    </div>
  )
}

// The hero product preview, with a gentle 3D tilt that follows the cursor.
// It's also a stagger child (fadeUp) — the entrance and the tilt share the
// element but touch different transform axes, so they compose cleanly.
function TiltPreview({ children }) {
  const reduce = useReducedMotion()
  const ref = useRef(null)
  const mx = useMotionValue(0.5)
  const my = useMotionValue(0.5)
  const rotateX = useSpring(useTransform(my, [0, 1], [5, -5]), spring.follow)
  const rotateY = useSpring(useTransform(mx, [0, 1], [-5, 5]), spring.follow)

  const onMove = (e) => {
    if (reduce || !ref.current) return
    const r = ref.current.getBoundingClientRect()
    mx.set((e.clientX - r.left) / r.width)
    my.set((e.clientY - r.top) / r.height)
  }
  const reset = () => { mx.set(0.5); my.set(0.5) }

  return (
    <motion.div
      ref={ref}
      className="preview"
      onMouseMove={onMove}
      onMouseLeave={reset}
      variants={fadeUp}
      style={{ rotateX, rotateY, transformPerspective: 1200 }}
    >
      {children}
    </motion.div>
  )
}

// A button that nudges slightly toward the cursor on hover, then springs back.
// Small, tasteful — the kind of micro-interaction that reads as "premium".
function MagneticCTA({ children, onClick, href, strength = 0.35 }) {
  const reduce = useReducedMotion()
  const ref = useRef(null)
  const x = useMotionValue(0)
  const y = useMotionValue(0)
  const sx = useSpring(x, spring.snappy)
  const sy = useSpring(y, spring.snappy)

  const onMove = (e) => {
    if (reduce || !ref.current) return
    const r = ref.current.getBoundingClientRect()
    x.set((e.clientX - (r.left + r.width / 2)) * strength)
    y.set((e.clientY - (r.top + r.height / 2)) * strength)
  }
  const reset = () => { x.set(0); y.set(0) }

  return (
    <motion.a
      ref={ref}
      className="btn btn-primary btn-lg"
      href={href}
      onClick={onClick}
      onMouseMove={onMove}
      onMouseLeave={reset}
      style={{ x: sx, y: sy }}
      whileHover={reduce ? undefined : { scale: 1.04 }}
      whileTap={{ scale: 0.97 }}
      transition={spring.snappy}
    >
      {children}
    </motion.a>
  )
}

// The YBase marketing landing page — the design system in its editorial
// register. Shown to logged-out visitors; CTAs lead into the product (sign in
// or start a workspace). Motion is powered by Framer Motion via ybase/motionPresets.
export default function Marketing({ onEnter }) {
  const rootRef = useRef(null)
  const reduce = useReducedMotion()
  const [scrolled, setScrolled] = useState(false)

  // The marketing page scrolls inside .wb-marketing (not the window), so the
  // scroll listener and whileInView observers are bound to that container.
  const { scrollY } = useScroll({ container: rootRef })
  useMotionValueEvent(scrollY, 'change', (v) => setScrolled(v > 8))
  const viewport = { ...inView, root: rootRef }

  const enter = (intent) => (e) => { e.preventDefault(); onEnter(intent) }
  const scrollTo = (id) => (e) => {
    e.preventDefault()
    rootRef.current?.querySelector(id)?.scrollIntoView({ behavior: 'smooth' })
  }

  const features = [
    { Icon: Quote, title: 'Full provenance', body: 'Every answer carries citation chips that open the original Slack message or doc, with the cited chunk highlighted.' },
    { Icon: RotateCcw, title: 'Revisit detection', body: 'When a new document relitigates a settled decision, YBase surfaces it — so you never re-argue what you already decided.' },
    { Icon: GitCommitHorizontal, title: 'Decision log', body: 'Positions on the record, alternatives considered, and the chain of revisits — for every decision your team has made.' },
    { Icon: Users, title: 'People pages', body: 'Everything someone advocated, decided or raised, with their recorded positions quoted verbatim.' },
    { Icon: ShieldCheck, title: 'Runs fully local', body: 'Ollama for LLM and embeddings, zero API keys — or Claude + Voyage when credentials are present. The switch is one env var.' },
    { Icon: Scale, title: 'Honest confidence', body: 'Answers score their own confidence and surface counter-evidence — a structured “not in memory” beats a confident guess.' },
  ]

  const steps = [
    { Icon: Download, n: '01', title: 'Ingest & dedup', body: 'Connect Slack, Notion, GitHub and Jira. Threads become documents; near-duplicates merge by content hash before anything is stored.' },
    { Icon: GitFork, n: '02', title: 'Form memory', body: 'An LLM extracts decisions, reasoning, advocates and alternatives — then links them into a typed graph: revisits, resolves, involves, about.' },
    { Icon: MessageSquareQuote, n: '03', title: 'Answer with provenance', body: 'Hybrid retrieval plus 2-hop graph expansion finds evidence others miss — and every claim is cited back to the source that settled it.' },
  ]

  return (
    <MotionConfig reducedMotion="user">
      <div className="wb-marketing" ref={rootRef}>
        <nav className={`nav${scrolled ? ' scrolled' : ''}`}>
          <div className="nav-inner">
            <a className="m-brand" onClick={enter('home')}><img src={ybaseMark} alt="" />YBase</a>
            <div className="nav-links">
              <a href="#how" onClick={scrollTo('#how')}>How it works</a>
              <a href="#features" onClick={scrollTo('#features')}>Features</a>
              <a href="#graph" onClick={scrollTo('#graph')}>Memory graph</a>
              <a href="#" onClick={(e) => e.preventDefault()}>Pricing</a>
            </div>
            <div className="nav-cta">
              <a className="btn btn-secondary" onClick={enter('login')}>Sign in</a>
              <a className="btn btn-primary" onClick={enter('signup')}>Start free</a>
            </div>
          </div>
        </nav>

        <section className="hero">
          <motion.div
            className="hero-aurora"
            aria-hidden="true"
            animate={reduce ? undefined : { x: [0, 34, -22, 0], y: [0, -16, 12, 0], scale: [1, 1.06, 1] }}
            transition={{ duration: 22, repeat: Infinity, ease: 'easeInOut' }}
          />
          <motion.div
            className="wrap"
            variants={staggerContainer(0.09, 0.05)}
            initial="hidden"
            animate="show"
          >
            <motion.span className="pill-link" variants={fadeUp}>
              <span className="tagk"><Sparkles size={13} strokeWidth={1.8} /> New</span>
              <b>Claude-powered</b> memory formation <ArrowRight size={15} strokeWidth={1.8} />
            </motion.span>
            <motion.h1 className="title" variants={fadeUp}>Never lose the <em>why</em> behind your team&apos;s decisions</motion.h1>
            <motion.p className="sub" variants={fadeUp}>
              YBase is an AI memory layer over Slack, Notion, GitHub and Jira. Not search — it
              remembers decisions and their reasoning, links them across sources and time, and answers
              “why” with full provenance.
            </motion.p>
            <motion.div className="hero-cta" variants={fadeUp}>
              <MagneticCTA onClick={enter('signup')}>Open the product <ArrowRight size={16} strokeWidth={1.8} /></MagneticCTA>
              <a className="btn btn-secondary btn-lg" href="#how" onClick={scrollTo('#how')}>See how it works</a>
            </motion.div>

            <TiltPreview>
              <div className="preview-bar">
                <span className="dot3" style={{ background: '#f0726c' }} />
                <span className="dot3" style={{ background: '#e0a849' }} />
                <span className="dot3" style={{ background: '#3ec98a' }} />
              </div>
              <div className="preview-body">
                <div className="q-line"><Sparkles size={15} strokeWidth={1.8} /> Ask memory</div>
                <div className="q-text">Why did we choose Postgres over MongoDB?</div>
                <div className="ans">
                  The team chose <b>PostgreSQL</b> as the single datastore for v1 — for transactional
                  integrity in billing<span className="cite">C1</span>, the team&apos;s operational
                  experience over a Mongo cluster<span className="cite">C1</span>, and because JSONB +
                  pgvector keep the stack to one database<span className="cite">C2</span>. Dev Patel
                  pushed for Mongo on the activity feed; it was revisited in Jan 2026 and{' '}
                  <b>reaffirmed</b>.<span className="cite">C3</span>
                </div>
                <div className="ans-meta">
                  <span className="conf">confidence: high</span>
                  <span className="src"><i style={{ background: 'var(--src-slack)' }} /> Slack</span>
                  <span className="src"><i style={{ background: 'var(--src-notion)' }} /> Notion</span>
                  <span className="src"><i style={{ background: 'var(--src-jira)' }} /> Jira</span>
                </div>
              </div>
            </TiltPreview>

            <motion.div className="trust" variants={fadeUp}>
              <span>Built for engineering teams who decide fast</span>
              <div className="logos">
                <span>Stripe</span><span>Vercel</span><span>Ramp</span><span>Linear</span><span>Notion</span>
              </div>
            </motion.div>
          </motion.div>
        </section>

        <section className="section-pad" id="how">
          <div className="wrap">
            <motion.span className="m-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>How it works</motion.span>
            <motion.h2 className="h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Memory, not search</motion.h2>
            <motion.p className="lede" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Every document your team writes flows through the same pipeline — ingested, understood, and connected into a graph you can ask questions of.</motion.p>
            <motion.div className="steps" variants={staggerContainer(0.12)} initial="hidden" whileInView="show" viewport={viewport}>
              {steps.map((s) => (
                <motion.div className="step" variants={fadeUp} key={s.n}>
                  <div className="step-n">{s.n}</div>
                  <div className="step-icon"><s.Icon size={20} strokeWidth={1.8} /></div>
                  <h3>{s.title}</h3>
                  <p>{s.body}</p>
                </motion.div>
              ))}
            </motion.div>
          </div>
        </section>

        <section className="section-pad" id="features" style={{ background: 'var(--surface-sunken)', borderBlock: '1px solid var(--border-subtle)' }}>
          <div className="wrap">
            <motion.span className="m-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Why teams trust it</motion.span>
            <motion.h2 className="h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>Answers you can defend</motion.h2>
            <motion.div className="features" variants={staggerContainer(0.08)} initial="hidden" whileInView="show" viewport={viewport}>
              {features.map((f) => (
                <motion.div
                  className="feat"
                  variants={fadeUp}
                  whileHover={reduce ? undefined : { y: -4 }}
                  transition={spring.soft}
                  key={f.title}
                >
                  <div className="feat-icon"><f.Icon size={22} strokeWidth={1.8} /></div>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </motion.div>
              ))}
            </motion.div>
          </div>
        </section>

        <section className="section-pad" id="graph">
          <div className="wrap split">
            <div>
              <motion.span className="m-eyebrow" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>The memory graph</motion.span>
              <motion.h2 className="h2" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>It connects what search can&apos;t</motion.h2>
              <motion.p className="lede" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
                A question that vector-matches the September Slack debate also surfaces the January Jira
                near-reversal — because the graph edge <code>revisits</code> connects them, even when the
                ticket shares few words with the question.
              </motion.p>
              <motion.div variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport} style={{ marginTop: 28 }}>
                <a className="btn btn-primary btn-lg" onClick={enter('signup')}>Explore the graph <ArrowRight size={16} strokeWidth={1.8} /></a>
              </motion.div>
            </div>
            <motion.div className="graph-card" initial="hidden" whileInView="show" viewport={viewport}>
              <motion.div className="gnode lead" variants={nodeV} custom={0}><GitCommitHorizontal size={17} strokeWidth={1.8} style={{ color: 'var(--accent-text)' }} /> Postgres for v1 <span className="gtag">decision</span></motion.div>
              <GConn index={0} pulse reduce={reduce} />
              <motion.div className="gnode" variants={nodeV} custom={1}><MessageSquare size={17} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)' }} /> #engineering debate <span className="gtag">revisits</span></motion.div>
              <GConn index={1} reduce={reduce} />
              <motion.div className="gnode" variants={nodeV} custom={2}><SquareCheck size={17} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)' }} /> ENG-481 near-reversal <span className="gtag">revisits</span></motion.div>
              <GConn index={2} pulse reduce={reduce} />
              <motion.div className="gnode" variants={nodeV} custom={3}><CircleHelp size={17} strokeWidth={1.8} style={{ color: 'var(--warning)' }} /> Read replicas before launch? <span className="gtag">opens</span></motion.div>
            </motion.div>
          </div>
        </section>

        <section className="section-pad">
          <div className="wrap">
            <motion.div className="cta" variants={fadeUp} initial="hidden" whileInView="show" viewport={viewport}>
              <h2>Stop re-arguing settled decisions</h2>
              <p>Give your team a memory that remembers the reasoning — not just the outcome. Set up in minutes, fully local or Claude-powered.</p>
              <div style={{ display: 'flex', gap: 14, justifyContent: 'center', flexWrap: 'wrap' }}>
                <button className="btn-white" onClick={() => onEnter('signup')}>Start free</button>
                <button className="btn-ghost-w" onClick={() => onEnter('login')}>Book a demo</button>
              </div>
            </motion.div>
          </div>
        </section>

        <footer>
          <div className="wrap foot-inner">
            <div className="foot-brand">
              <div className="m-brand"><img src={ybaseMark} alt="" />YBase</div>
              <p>An AI memory layer for engineering teams. Never lose the why behind your decisions.</p>
            </div>
            <div className="foot-cols">
              <div className="foot-col"><h4>Product</h4><a onClick={enter('login')}>Ask memory</a><a onClick={enter('login')}>Decision log</a><a onClick={enter('login')}>Memory graph</a><a onClick={enter('login')}>Integrations</a></div>
              <div className="foot-col"><h4>Company</h4><a onClick={(e) => e.preventDefault()}>About</a><a onClick={(e) => e.preventDefault()}>Careers</a><a onClick={(e) => e.preventDefault()}>Security</a><a onClick={(e) => e.preventDefault()}>Contact</a></div>
              <div className="foot-col"><h4>Resources</h4><a onClick={(e) => e.preventDefault()}>Docs</a><a onClick={(e) => e.preventDefault()}>Changelog</a><a onClick={(e) => e.preventDefault()}>Self-host</a></div>
            </div>
          </div>
        </footer>
      </div>
    </MotionConfig>
  )
}
