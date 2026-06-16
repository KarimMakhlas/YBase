import React, { useEffect, useRef } from 'react'
import {
  Sparkles, ArrowRight, Download, GitFork, MessageSquareQuote, Quote, RotateCcw,
  GitCommitHorizontal, Users, ShieldCheck, Scale, MessageSquare, SquareCheck, CircleHelp,
} from 'lucide-react'
import whybaseMark from '../assets/whybase-mark.svg'
import '../whybase/marketing.css'

// The WhyBase marketing landing page — the design system in its editorial
// register. Shown to logged-out visitors; CTAs lead into the product (sign in
// or start a workspace). Ported from ui_kits/marketing.
export default function Marketing({ onEnter }) {
  const rootRef = useRef(null)

  useEffect(() => {
    const els = rootRef.current?.querySelectorAll('.reveal') || []
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target) } }),
      { threshold: 0.12, rootMargin: '0px 0px -8% 0px' },
    )
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])

  const enter = (intent) => (e) => { e.preventDefault(); onEnter(intent) }
  const scrollTo = (id) => (e) => {
    e.preventDefault()
    rootRef.current?.querySelector(id)?.scrollIntoView({ behavior: 'smooth' })
  }

  const features = [
    { Icon: Quote, title: 'Full provenance', body: 'Every answer carries citation chips that open the original Slack message or doc, with the cited chunk highlighted.' },
    { Icon: RotateCcw, title: 'Revisit detection', body: 'When a new document relitigates a settled decision, WhyBase surfaces it — so you never re-argue what you already decided.' },
    { Icon: GitCommitHorizontal, title: 'Decision log', body: 'Positions on the record, alternatives considered, and the chain of revisits — for every decision your team has made.' },
    { Icon: Users, title: 'People pages', body: 'Everything someone advocated, decided or raised, with their recorded positions quoted verbatim.' },
    { Icon: ShieldCheck, title: 'Runs fully local', body: 'Ollama for LLM and embeddings, zero API keys — or Claude + Voyage when credentials are present. The switch is one env var.' },
    { Icon: Scale, title: 'Honest confidence', body: 'Answers score their own confidence and surface counter-evidence — a structured “not in memory” beats a confident guess.' },
  ]

  return (
    <div className="wb-marketing" ref={rootRef}>
      <nav className="nav">
        <div className="nav-inner">
          <a className="m-brand" onClick={enter('home')}><img src={whybaseMark} alt="" />WhyBase</a>
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
        <div className="wrap">
          <span className="pill-link reveal">
            <span className="tagk"><Sparkles size={13} strokeWidth={1.8} /> New</span>
            <b>Claude-powered</b> memory formation <ArrowRight size={15} strokeWidth={1.8} />
          </span>
          <h1 className="title reveal" style={{ '--d': '60ms' }}>Never lose the <em>why</em> behind your team&apos;s decisions</h1>
          <p className="sub reveal" style={{ '--d': '120ms' }}>
            WhyBase is an AI memory layer over Slack, Notion, GitHub and Jira. Not search — it
            remembers decisions and their reasoning, links them across sources and time, and answers
            “why” with full provenance.
          </p>
          <div className="hero-cta reveal" style={{ '--d': '180ms' }}>
            <a className="btn btn-primary btn-lg" onClick={enter('signup')}>Open the product <ArrowRight size={16} strokeWidth={1.8} /></a>
            <a className="btn btn-secondary btn-lg" href="#how" onClick={scrollTo('#how')}>See how it works</a>
          </div>

          <div className="preview reveal" style={{ '--d': '240ms' }}>
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
          </div>

          <div className="trust reveal" style={{ '--d': '300ms' }}>
            <span>Built for engineering teams who decide fast</span>
            <div className="logos">
              <span>Stripe</span><span>Vercel</span><span>Ramp</span><span>Linear</span><span>Notion</span>
            </div>
          </div>
        </div>
      </section>

      <section className="section-pad" id="how">
        <div className="wrap">
          <span className="m-eyebrow reveal">How it works</span>
          <h2 className="h2 reveal" style={{ '--d': '60ms' }}>Memory, not search</h2>
          <p className="lede reveal" style={{ '--d': '100ms' }}>Every document your team writes flows through the same pipeline — ingested, understood, and connected into a graph you can ask questions of.</p>
          <div className="steps">
            <div className="step reveal" style={{ '--d': '120ms' }}>
              <div className="step-n">01</div>
              <div className="step-icon"><Download size={20} strokeWidth={1.8} /></div>
              <h3>Ingest &amp; dedup</h3>
              <p>Connect Slack, Notion, GitHub and Jira. Threads become documents; near-duplicates merge by content hash before anything is stored.</p>
            </div>
            <div className="step reveal" style={{ '--d': '200ms' }}>
              <div className="step-n">02</div>
              <div className="step-icon"><GitFork size={20} strokeWidth={1.8} /></div>
              <h3>Form memory</h3>
              <p>An LLM extracts decisions, reasoning, advocates and alternatives — then links them into a typed graph: revisits, resolves, involves, about.</p>
            </div>
            <div className="step reveal" style={{ '--d': '280ms' }}>
              <div className="step-n">03</div>
              <div className="step-icon"><MessageSquareQuote size={20} strokeWidth={1.8} /></div>
              <h3>Answer with provenance</h3>
              <p>Hybrid retrieval plus 2-hop graph expansion finds evidence others miss — and every claim is cited back to the source that settled it.</p>
            </div>
          </div>
        </div>
      </section>

      <section className="section-pad" id="features" style={{ background: 'var(--surface-sunken)', borderBlock: '1px solid var(--border-subtle)' }}>
        <div className="wrap">
          <span className="m-eyebrow reveal">Why teams trust it</span>
          <h2 className="h2 reveal" style={{ '--d': '60ms' }}>Answers you can defend</h2>
          <div className="features">
            {features.map((f, i) => (
              <div className="feat reveal" style={{ '--d': `${80 + (i % 3) * 60}ms` }} key={f.title}>
                <div className="feat-icon"><f.Icon size={22} strokeWidth={1.8} /></div>
                <h3>{f.title}</h3>
                <p>{f.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="section-pad" id="graph">
        <div className="wrap split">
          <div>
            <span className="m-eyebrow reveal">The memory graph</span>
            <h2 className="h2 reveal" style={{ '--d': '60ms' }}>It connects what search can&apos;t</h2>
            <p className="lede reveal" style={{ '--d': '100ms' }}>
              A question that vector-matches the September Slack debate also surfaces the January Jira
              near-reversal — because the graph edge <code>revisits</code> connects them, even when the
              ticket shares few words with the question.
            </p>
            <div className="reveal" style={{ '--d': '140ms', marginTop: 28 }}>
              <a className="btn btn-primary btn-lg" onClick={enter('signup')}>Explore the graph <ArrowRight size={16} strokeWidth={1.8} /></a>
            </div>
          </div>
          <div className="graph-card reveal" style={{ '--d': '160ms' }}>
            <div className="gnode lead"><GitCommitHorizontal size={17} strokeWidth={1.8} style={{ color: 'var(--accent-text)' }} /> Postgres for v1 <span className="gtag">decision</span></div>
            <div className="gconn" />
            <div className="gnode"><MessageSquare size={17} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)' }} /> #engineering debate <span className="gtag">revisits</span></div>
            <div className="gconn" />
            <div className="gnode"><SquareCheck size={17} strokeWidth={1.8} style={{ color: 'var(--text-tertiary)' }} /> ENG-481 near-reversal <span className="gtag">revisits</span></div>
            <div className="gconn" />
            <div className="gnode"><CircleHelp size={17} strokeWidth={1.8} style={{ color: 'var(--warning)' }} /> Read replicas before launch? <span className="gtag">opens</span></div>
          </div>
        </div>
      </section>

      <section className="section-pad">
        <div className="wrap">
          <div className="cta reveal">
            <h2>Stop re-arguing settled decisions</h2>
            <p>Give your team a memory that remembers the reasoning — not just the outcome. Set up in minutes, fully local or Claude-powered.</p>
            <div style={{ display: 'flex', gap: 14, justifyContent: 'center', flexWrap: 'wrap' }}>
              <button className="btn-white" onClick={() => onEnter('signup')}>Start free</button>
              <button className="btn-ghost-w" onClick={() => onEnter('login')}>Book a demo</button>
            </div>
          </div>
        </div>
      </section>

      <footer>
        <div className="wrap foot-inner">
          <div className="foot-brand">
            <div className="m-brand"><img src={whybaseMark} alt="" />WhyBase</div>
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
  )
}
