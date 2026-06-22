import React, { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { motion, MotionConfig } from 'framer-motion'
import {
  House, Sparkles, Clock, GitCommitHorizontal, Users, Share2, FilePlus2,
  ListChecks, Flag, Plug, BarChart3, Gauge, Settings as SettingsIcon,
  Search, LogOut, Sun, Moon, PanelLeftClose, PanelLeftOpen, ChevronsUpDown, UserPlus,
} from 'lucide-react'
import Home from './components/Home.jsx'
import Chat from './components/Chat.jsx'
import Timeline from './components/Timeline.jsx'
import Decisions from './components/Decisions.jsx'
import Graph from './components/Graph.jsx'
import People from './components/People.jsx'
import AddMemory from './components/AddMemory.jsx'
import Auth from './components/Auth.jsx'
import Onboarding from './components/Onboarding.jsx'
import InviteModal from './components/InviteModal.jsx'
import Join from './components/Join.jsx'
import SharedDecision from './components/SharedDecision.jsx'
import Notifications from './components/Notifications.jsx'
import Marketing from './components/Marketing.jsx'
import Settings from './components/Settings.jsx'
import Sources from './components/Sources.jsx'
import Review from './components/Review.jsx'
import Feedback from './components/Feedback.jsx'
import Ops from './components/Ops.jsx'
import Analytics from './components/Analytics.jsx'
import CmdK from './components/CmdK.jsx'
import DocModal from './components/DocModal.jsx'
import StatusFooter from './components/StatusFooter.jsx'
import Plans from './components/Plans.jsx'
import BillingBanner from './components/BillingBanner.jsx'
import Account from './components/Account.jsx'
import { ToastProvider } from './components/Toast.jsx'
import { getBootstrapStatus, getMe, getOnboarding, getBillingStatus, logout } from './api.js'
import ybaseMark from './assets/ybase-mark.svg'

// The product surface, as a grouped left-sidebar nav (Linear-style). `section`
// places an item under a heading; the first two items sit above all sections.
// `minRole` keeps the existing role gating.
const NAV = [
  { id: 'home', label: 'Home', icon: House },
  { id: 'chat', label: 'Ask memory', icon: Sparkles },
  { id: 'timeline', label: 'Timeline', icon: Clock, section: 'Memory' },
  { id: 'decisions', label: 'Decision log', icon: GitCommitHorizontal, section: 'Memory' },
  { id: 'people', label: 'People', icon: Users, section: 'Memory' },
  { id: 'graph', label: 'Graph', icon: Share2, section: 'Memory' },
  { id: 'add', label: 'Add to memory', icon: FilePlus2, section: 'Curate', minRole: 'admin' },
  { id: 'review', label: 'Review', icon: ListChecks, section: 'Curate', minRole: 'admin' },
  { id: 'feedback', label: 'Feedback', icon: Flag, section: 'Curate', minRole: 'admin' },
  { id: 'sources', label: 'Sources', icon: Plug, section: 'Workspace', minRole: 'admin' },
  { id: 'analytics', label: 'Analytics', icon: BarChart3, section: 'Workspace', minRole: 'admin' },
  { id: 'ops', label: 'Ops', icon: Gauge, section: 'Workspace', minRole: 'admin' },
  { id: 'settings', label: 'Settings', icon: SettingsIcon, section: 'Workspace', minRole: 'admin' },
]

// Routable but not sidebar items: plans/billing (from the billing banner or
// account page) and account (from the workspace switcher in the sidebar footer).
const EXTRA_TABS = new Set(['plans', 'account'])
const LABELS = { ...Object.fromEntries(NAV.map((n) => [n.id, n.label])), plans: 'Billing', account: 'Account' }
const TAB_IDS = new Set([...NAV.map((t) => t.id), ...EXTRA_TABS])
const ROLE_RANK = { member: 1, admin: 2, owner: 3 }

// Smoothly cross-fade between views using the View Transitions API when available.
// flushSync forces React to apply the DOM update inside the transition snapshot.
function withViewTransition(fn) {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  if (document.startViewTransition && !reduce) {
    document.startViewTransition(() => flushSync(fn))
  } else {
    fn()
  }
}

function parseHashRoute() {
  const raw = window.location.hash.replace(/^#\/?/, '')
  if (!raw) return { tab: 'home', payload: {} }
  const [path, query = ''] = raw.split('?')
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent)
  const params = new URLSearchParams(query)
  const tab = parts[0] || 'home'

  if (tab === 'join' && parts[1]) {
    return { tab: null, payload: {}, joinToken: parts[1] }
  }

  if (tab === 'shared' && parts[1]) {
    return { tab: null, payload: {}, shareToken: parts[1] }
  }

  if (tab === 'reset' && parts[1]) {
    return { tab: null, payload: {}, resetToken: parts[1] }
  }

  if (tab === 'documents' && parts[1]) {
    return {
      tab: null,
      payload: {},
      docModal: {
        docId: Number(parts[1]),
        highlight: params.get('highlight') || null,
      },
    }
  }

  if (!TAB_IDS.has(tab)) return { tab: 'home', payload: {} }
  const payload = {}
  if (tab === 'decisions') {
    if (parts[1]) payload.decisionId = Number(parts[1])
    if (params.get('topic')) payload.topic = params.get('topic')
  } else if (tab === 'people' && parts[1]) {
    payload.personId = Number(parts[1])
  } else if (tab === 'graph' && parts[1]) {
    payload.nodeId = Number(parts[1])
  } else if (tab === 'review' && parts[1]) {
    payload.nodeId = Number(parts[1])
  } else if (tab === 'timeline' && params.get('focus')) {
    payload.focusKey = params.get('focus')
  }
  return { tab, payload }
}

function routeHash(toTab, payload = {}) {
  if (toTab === 'decisions' && payload.decisionId) return `#/decisions/${payload.decisionId}`
  if (toTab === 'decisions' && payload.topic) return `#/decisions?topic=${encodeURIComponent(payload.topic)}`
  if (toTab === 'people' && payload.personId) return `#/people/${payload.personId}`
  if (toTab === 'graph' && payload.nodeId) return `#/graph/${payload.nodeId}`
  if (toTab === 'review' && payload.nodeId) return `#/review/${payload.nodeId}`
  if (toTab === 'timeline' && payload.focusKey) return `#/timeline?focus=${encodeURIComponent(payload.focusKey)}`
  return `#/${TAB_IDS.has(toTab) ? toTab : 'home'}`
}

function documentHash(docId, highlight = null) {
  const extra = highlight ? `?highlight=${encodeURIComponent(highlight)}` : ''
  return `#/documents/${docId}${extra}`
}

// Deterministic avatar color from a name (workspace switcher).
function avatarHue(name = '') {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return h
}

function WorkspaceAvatar({ name }) {
  const initials = (name || '?')
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()
  const hue = avatarHue(name)
  return (
    <span
      className="wb-avatar wb-avatar--sm"
      aria-hidden="true"
      style={{ background: `linear-gradient(150deg, hsl(${hue} 52% 56%), hsl(${(hue + 38) % 360} 54% 42%))` }}
    >
      {initials}
    </span>
  )
}

const THEME_KEY = 'sb:theme'

function resolveTheme() {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(theme) {
  if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
}

function ThemeToggle() {
  const [theme, setTheme] = useState(resolveTheme)

  // Track OS changes only while the user hasn't set an explicit preference.
  // We must set the attribute ourselves now that the OS-dark CSS media query
  // is gone (theme tokens live only under :root and :root[data-theme="dark"]).
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!mq) return
    const onChange = () => {
      if (!localStorage.getItem(THEME_KEY)) {
        const next = mq.matches ? 'dark' : 'light'
        applyTheme(next)
        setTheme(next)
      }
    }
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])

  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem(THEME_KEY, next)
    applyTheme(next)
    setTheme(next)
  }

  const isDark = theme === 'dark'
  return (
    <button
      className="wb-iconbtn"
      onClick={toggle}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {isDark ? <Sun size={17} strokeWidth={1.8} /> : <Moon size={17} strokeWidth={1.8} />}
    </button>
  )
}

export default function App() {
  const [tab, setTab] = useState('home')
  const [authState, setAuthState] = useState({ loading: true, needsBootstrap: false, user: null })
  // bumping `ask.n` lets Home re-send even the same question text
  const [ask, setAsk] = useState({ question: null, n: 0 })
  // cross-view navigation payload: views read it when focus.tab matches
  const [focus, setFocus] = useState({ tab: null, n: 0 })
  const [cmdkOpen, setCmdkOpen] = useState(false)
  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem('wb:nav') === 'collapsed',
  )
  const [docModal, setDocModal] = useState(null) // { docId, highlight }
  const [joinToken, setJoinToken] = useState(null) // invite link: #/join/<token>
  const [shareToken, setShareToken] = useState(null) // public decision: #/shared/<token>
  const [resetToken, setResetToken] = useState(null) // password reset: #/reset/<token>
  // Keeps the setup wizard mounted across workspace creation (once a user enters
  // onboarding, their workspace becomes non-null mid-flow — this flag prevents an
  // early jump into the main app until they finish or skip).
  const [onboarding, setOnboarding] = useState(false)
  const [inviteOpen, setInviteOpen] = useState(false)
  const [setup, setSetup] = useState(null) // GET /api/workspace/onboarding: checklist state
  const [billing, setBilling] = useState(null) // GET /api/billing/status: trial/plan state
  const lastNonDocHash = React.useRef('#/home')

  const loadSetup = React.useCallback(() => {
    getOnboarding().then(setSetup).catch(() => setSetup(null))
  }, [])
  const loadBilling = React.useCallback(() => {
    getBillingStatus().then(setBilling).catch(() => setBilling(null))
  }, [])
  const currentTabRef = React.useRef('home')

  const loadAuth = async () => {
    try {
      const boot = await getBootstrapStatus()
      if (boot.needs_bootstrap) {
        setAuthState({ loading: false, needsBootstrap: true, user: null })
        return
      }
      const me = await getMe()
      setAuthState({ loading: false, needsBootstrap: false, user: me })
    } catch {
      setAuthState({ loading: false, needsBootstrap: false, user: null })
    }
  }

  useEffect(() => {
    loadAuth()
    const applyRoute = () => {
      const route = parseHashRoute()
      if (route.joinToken) {
        setJoinToken(route.joinToken)
        return
      }
      setJoinToken(null)
      if (route.shareToken) {
        setShareToken(route.shareToken)
        return
      }
      setShareToken(null)
      if (route.resetToken) {
        setResetToken(route.resetToken)
        return
      }
      setResetToken(null)
      if (route.docModal?.docId) {
        setDocModal(route.docModal)
        return
      }
      const nextTab = route.tab || 'home'
      lastNonDocHash.current = routeHash(nextTab, route.payload)
      const tabChanged = currentTabRef.current !== nextTab
      currentTabRef.current = nextTab
      const apply = () => {
        setDocModal(null)
        setFocus((f) => ({ tab: nextTab, n: f.n + 1, ...route.payload }))
        setTab(nextTab)
      }
      if (tabChanged) withViewTransition(apply)
      else apply()
    }
    applyRoute()
    window.addEventListener('hashchange', applyRoute)
    const onRequired = () =>
      setAuthState((s) => ({ ...s, loading: false, needsBootstrap: false, user: null }))
    window.addEventListener('auth:required', onRequired)
    return () => {
      window.removeEventListener('hashchange', applyRoute)
      window.removeEventListener('auth:required', onRequired)
    }
  }, [])

  useEffect(() => {
    document.title = LABELS[tab] ? `YBase — ${LABELS[tab]}` : 'YBase'
  }, [tab])

  // Setup-checklist + billing state, refetched whenever the active workspace changes.
  const workspaceId = authState.user?.workspace?.id
  useEffect(() => {
    if (workspaceId) { loadSetup(); loadBilling() }
    else { setSetup(null); setBilling(null) }
  }, [workspaceId, loadSetup, loadBilling])

  // A write blocked by the server (402) re-checks billing so the banner flips
  // to read-only without a manual refresh.
  useEffect(() => {
    const onReadonly = () => loadBilling()
    window.addEventListener('billing:readonly', onReadonly)
    return () => window.removeEventListener('billing:readonly', onReadonly)
  }, [loadBilling])

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCmdkOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const askFromHome = (question) => {
    setAsk((a) => ({ question, n: a.n + 1 }))
    navigate('chat')
  }

  const navigate = (toTab, payload = {}) => {
    const next = routeHash(toTab, payload)
    if (window.location.hash === next) {
      setFocus((f) => ({ tab: toTab, n: f.n + 1, ...payload }))
      setTab(toTab)
    } else {
      window.location.hash = next
    }
  }

  const toggleNav = () =>
    setNavCollapsed((c) => {
      const next = !c
      try { localStorage.setItem('wb:nav', next ? 'collapsed' : 'expanded') } catch { /* ignore */ }
      return next
    })

  const openDoc = (docId, highlight = null) => {
    const next = documentHash(docId, highlight)
    if (window.location.hash === next) setDocModal({ docId, highlight })
    else window.location.hash = next
  }

  const closeDoc = () => {
    setDocModal(null)
    if (window.location.hash.startsWith('#/documents/')) {
      window.history.replaceState(null, '', lastNonDocHash.current || '#/home')
    }
  }

  const onPick = (item) => {
    if (item.type === 'decision') navigate('decisions', { decisionId: item.id })
    else if (item.type === 'question') navigate('timeline', { focusKey: `question-${item.id}` })
    else if (item.type === 'entity') navigate('people', { personId: item.id })
    else if (item.type === 'topic') navigate('decisions', { topic: item.title })
    else if (item.type === 'document') openDoc(item.id)
  }

  const onLogout = async () => {
    try { await logout() } catch { /* already gone */ }
    setAuthState({ loading: false, needsBootstrap: false, user: null })
    navigate('home')
  }

  if (shareToken) {
    return (
      <ToastProvider>
        <SharedDecision token={shareToken} />
      </ToastProvider>
    )
  }

  if (resetToken) {
    return (
      <ToastProvider>
        <Auth
          key="reset-auth"
          mode="login"
          resetToken={resetToken}
          onAuthed={(user) => {
            setAuthState({ loading: false, needsBootstrap: false, user })
            setResetToken(null)
            navigate('home')
          }}
          onBack={() => { setResetToken(null); window.location.hash = '#/login' }}
        />
      </ToastProvider>
    )
  }

  if (joinToken) {
    return (
      <ToastProvider>
        <Join
          token={joinToken}
          onJoined={(user) => {
            setAuthState({ loading: false, needsBootstrap: false, user })
            setJoinToken(null)
            navigate('home')
          }}
          onCancel={() => {
            setJoinToken(null)
            window.location.hash = '#/home'
          }}
        />
      </ToastProvider>
    )
  }

  if (authState.loading) {
    return (
      <ToastProvider>
        <div className="auth-page"><div className="skeleton skel-row auth-loading" /></div>
      </ToastProvider>
    )
  }

  if (authState.needsBootstrap || !authState.user) {
    // Logged-out surface. First run → locked bootstrap. Otherwise the marketing
    // landing page is the public entry; #/login and #/signup open the auth card.
    const hash = window.location.hash
    const wantsAuth = /^#\/(login|signup)/.test(hash)
    const onAuthed = (user) => {
      setAuthState({ loading: false, needsBootstrap: false, user })
      navigate('home')
    }
    if (!authState.needsBootstrap && !wantsAuth) {
      return (
        <ToastProvider>
          <Marketing onEnter={(intent) => {
            window.location.hash = intent === 'signup' ? '#/signup' : intent === 'login' ? '#/login' : '#/login'
          }} />
        </ToastProvider>
      )
    }
    return (
      <ToastProvider>
        <Auth
          key="login-auth"
          mode={authState.needsBootstrap ? 'bootstrap' : 'login'}
          initialView={hash.startsWith('#/signup') ? 'register' : 'login'}
          onBack={() => { window.location.hash = '#/welcome' }}
          onAuthed={onAuthed}
        />
      </ToastProvider>
    )
  }

  // Onboarding: an authenticated user with no workspace yet (fresh signup) must
  // run the setup wizard before reaching the app. `onboarding` keeps them there
  // through workspace creation until they finish or skip.
  if (!authState.user.workspace || onboarding) {
    return (
      <ToastProvider>
        <Onboarding
          user={authState.user}
          onWorkspaceCreated={(payload) => {
            setOnboarding(true)
            setAuthState({ loading: false, needsBootstrap: false, user: payload })
          }}
          onFinish={() => {
            setOnboarding(false)
            navigate('home')
          }}
          onLogout={onLogout}
        />
      </ToastProvider>
    )
  }

  const workspace = authState.user.workspace
  const role = workspace.role
  const isAdmin = ROLE_RANK[role] >= ROLE_RANK.admin
  const visibleNav = NAV.filter((n) => !n.minRole || ROLE_RANK[role] >= ROLE_RANK[n.minRole])
  const activeTab =
    visibleNav.some((n) => n.id === tab) || EXTRA_TABS.has(tab) ? tab : 'home'

  // Group visible items by section (preserving order); items without a section
  // render first, headerless.
  const sections = []
  for (const n of visibleNav) {
    const label = n.section || ''
    let s = sections.find((x) => x.label === label)
    if (!s) { s = { label, items: [] }; sections.push(s) }
    s.items.push(n)
  }

  return (
    <ToastProvider>
      <div className={`wb-app ${navCollapsed ? 'nav-collapsed' : ''}`}>
        <aside className="app-sidebar">
          <div className="sidebar-top">
            <div className="app-brand">
              <img src={ybaseMark} alt="" width="24" height="24" />
              <span>YBase</span>
            </div>
            <button
              className="nav-toggle"
              onClick={toggleNav}
              aria-label="Toggle sidebar"
              title={navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            >
              {navCollapsed ? <PanelLeftOpen size={17} strokeWidth={1.8} /> : <PanelLeftClose size={17} strokeWidth={1.8} />}
            </button>
          </div>

          <button className="sidebar-search" onClick={() => setCmdkOpen(true)} title="Search memory (⌘K)">
            <Search size={16} strokeWidth={1.8} />
            <span className="search-txt">Search memory</span>
            <span className="kbd">⌘K</span>
          </button>

          <MotionConfig reducedMotion="user">
            <nav className="sidebar-nav">
              {sections.map((s, si) => (
                <div className="nav-group" key={s.label || `top-${si}`}>
                  {s.label && <div className="nav-group-label">{s.label}</div>}
                  {s.items.map((n) => {
                    const Icon = n.icon
                    const isActive = activeTab === n.id
                    return (
                      <button
                        key={n.id}
                        className={isActive ? 'nav-item active' : 'nav-item'}
                        onClick={() => navigate(n.id)}
                        title={n.label}
                      >
                        {isActive && (
                          <motion.span
                            layoutId="nav-active"
                            className="nav-active-bg"
                            transition={{ type: 'spring', stiffness: 520, damping: 42 }}
                          />
                        )}
                        <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
                        <span>{n.label}</span>
                      </button>
                    )
                  })}
                </div>
              ))}
            </nav>
          </MotionConfig>

          <div className="sidebar-foot">
            {isAdmin && setup && !setup.complete && (
              <button className="setup-nudge" onClick={() => navigate('home')} title="Finish workspace setup">
                <ListChecks size={15} strokeWidth={1.8} />
                <span>Finish setup</span>
                <span className="setup-nudge-count tnum">
                  {Object.values(setup.steps).filter(Boolean).length}/{Object.keys(setup.steps).length}
                </span>
              </button>
            )}
            <button className="ws-switch" title="Account & workspaces" onClick={() => navigate('account')}>
              <WorkspaceAvatar name={workspace.name} />
              <span className="ws-meta">
                <b>{workspace.name}</b>
                <small>{authState.user.display_name} · {role}</small>
              </span>
              <ChevronsUpDown size={15} strokeWidth={1.8} className="ws-switch-chev" aria-hidden="true" />
            </button>
            <button className="logout-btn" onClick={onLogout} title="Log out">
              <LogOut size={16} strokeWidth={1.8} />
              <span>Log out</span>
            </button>
          </div>
        </aside>

        <div className="app-main">
          <header className="app-topbar">
            <div className="topbar-crumb">
              <span className="eyebrow">Workspace · {workspace.name}</span>
              <span className="crumb-view">{LABELS[activeTab] || 'Home'}</span>
            </div>
            <div className="app-top-right">
              <ThemeToggle />
              <Notifications isAdmin={isAdmin} />
              {isAdmin && (
                <button className="wb-btn wb-btn--secondary" onClick={() => setInviteOpen(true)}>
                  <UserPlus size={15} strokeWidth={1.8} /> Invite
                </button>
              )}
              <button className="wb-btn wb-btn--primary" onClick={() => navigate('chat')}>
                <Sparkles size={15} strokeWidth={1.8} /> Ask memory
              </button>
            </div>
          </header>

          <BillingBanner billing={billing} activeTab={activeTab} onUpgrade={() => navigate('plans')} />

          <main className="app-content content">
            {activeTab === 'plans' && (
              <Plans
                billing={billing}
                canPay={role === 'owner'}
                onUpgraded={() => { loadBilling(); loadAuth() }}
                onBack={() => navigate('home')}
              />
            )}
            {activeTab === 'account' && (
              <Account
                user={authState.user}
                onAuthChanged={() => { loadAuth(); loadSetup(); loadBilling() }}
                onNavigate={navigate}
                onBack={() => navigate('home')}
              />
            )}
            {activeTab === 'home' && (
              <Home
                onAsk={askFromHome}
                onNavigate={navigate}
                canAdmin={isAdmin}
                workspace={workspace}
                user={authState.user}
                setup={setup}
                onInvite={() => setInviteOpen(true)}
              />
            )}
            <div style={{ display: activeTab === 'chat' ? 'block' : 'none', height: '100%' }}>
              <Chat pendingAsk={ask} canAdd={isAdmin} onAddDoc={() => navigate('add')} onOpenDoc={openDoc} onNavigate={navigate} />
            </div>
            {activeTab === 'timeline' && (
              <Timeline focus={focus.tab === 'timeline' ? focus : null} onOpenDoc={openDoc} />
            )}
            {activeTab === 'decisions' && (
              <Decisions focus={focus.tab === 'decisions' ? focus : null} onOpenDoc={openDoc} />
            )}
            {activeTab === 'people' && (
              <People
                focus={focus.tab === 'people' ? focus : null}
                onNavigate={navigate}
                onOpenDoc={openDoc}
              />
            )}
            {activeTab === 'graph' && (
              <Graph focus={focus.tab === 'graph' ? focus : null} onOpenDoc={openDoc} />
            )}
            {activeTab === 'analytics' && <Analytics />}
            {activeTab === 'ops' && <Ops onNavigate={navigate} onAsk={askFromHome} />}
            {activeTab === 'review' && <Review focus={focus.tab === 'review' ? focus : null} />}
            {activeTab === 'feedback' && <Feedback onOpenDoc={openDoc} onNavigate={navigate} />}
            {activeTab === 'sources' && <Sources />}
            {activeTab === 'add' && <AddMemory />}
            {activeTab === 'settings' && (
              <Settings auth={authState.user} onAuthChanged={() => { loadAuth(); loadSetup() }} />
            )}
          </main>

          {isAdmin && <StatusFooter />}
        </div>

        {inviteOpen && (
          <InviteModal
            workspaceName={workspace.name}
            onClose={() => { setInviteOpen(false); loadSetup() }}
            onNavigateSettings={() => navigate('settings')}
          />
        )}

        <CmdK open={cmdkOpen} onClose={() => setCmdkOpen(false)} onPick={onPick} />
        {docModal && (
          <DocModal
            docId={docModal.docId}
            highlight={docModal.highlight}
            onClose={closeDoc}
          />
        )}
      </div>
    </ToastProvider>
  )
}
