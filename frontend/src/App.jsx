import React, { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  MessageSquare, GitCommitHorizontal, Plug, Settings as SettingsIcon,
  PanelLeftClose, PanelLeftOpen, Sun, Moon,
} from 'lucide-react'
import Chat from './components/Chat.jsx'
import Decisions from './components/Decisions.jsx'
import Auth from './components/Auth.jsx'
import Onboarding from './components/Onboarding.jsx'
import AccountMenu from './components/AccountMenu.jsx'
import Marketing from './components/Marketing.jsx'
import Settings from './components/Settings.jsx'
import Sources from './components/Sources.jsx'
import CmdK from './components/CmdK.jsx'
import DocModal from './components/DocModal.jsx'
import Plans from './components/Plans.jsx'
import Account from './components/Account.jsx'
import VerifyEmail from './components/VerifyEmail.jsx'
import VerifyBanner from './components/VerifyBanner.jsx'
import { ToastProvider } from './components/Toast.jsx'
import { getBootstrapStatus, getMe, getOnboarding, getBillingStatus, logout } from './api.js'

const PAGE_DEFS = {
  pulse: { label: 'Ask', Icon: MessageSquare },
  decisions: { label: 'Decisions', Icon: GitCommitHorizontal },
  sources: { label: 'Sources', Icon: Plug },
  settings: { label: 'Settings', Icon: SettingsIcon },
}
const MEMBER_PAGES = ['pulse', 'decisions']
const ADMIN_PAGES = ['pulse', 'decisions', 'sources', 'settings']
// Reachable from the menu but never shown as a nav pill.
const EXTRA_PAGES = new Set(['account', 'plans'])
const ADMIN_ONLY = new Set(['sources', 'settings'])
const ALL_RENDERABLE = new Set([...ADMIN_PAGES, ...EXTRA_PAGES])

const TAB_IDS = new Set([
  'pulse', 'chat', 'home', 'decisions', 'sources',
  'settings', 'account', 'plans', 'timeline', 'graph', 'add',
])
const ROLE_RANK = { member: 1, admin: 2, owner: 3 }

const SPLIT_KEY = 'sb:splitRatio'
const COLLAPSE_KEY = 'sb:leftCollapsed'
const SPLIT_MIN = 0.26
const SPLIT_MAX = 0.62

// Map any incoming tab onto a page we actually render. Cut surfaces fold into
// the nearest survivor (graph/timeline → decisions, add → sources).
function mapTab(tab) {
  let t = tab === 'home' || tab === 'chat' ? 'pulse' : tab
  if (t === 'add') t = 'sources'
  if (t === 'timeline' || t === 'graph') t = 'decisions'
  if (!ALL_RENDERABLE.has(t)) t = 'pulse'
  return t
}

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
  if (!raw) return { tab: 'chat', payload: {} }
  const [path, query = ''] = raw.split('?')
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent)
  const params = new URLSearchParams(query)
  const tab = parts[0] || 'chat'

  if (tab === 'reset' && parts[1]) return { tab: null, payload: {}, resetToken: parts[1] }
  if (tab === 'verify' && parts[1]) return { tab: null, payload: {}, verifyToken: parts[1] }
  if (tab === 'documents' && parts[1]) {
    return { tab: null, payload: {}, docModal: { docId: Number(parts[1]), highlight: params.get('highlight') || null } }
  }

  if (!TAB_IDS.has(tab)) return { tab: 'chat', payload: {} }
  const payload = {}
  if (tab === 'decisions') {
    if (parts[1]) payload.decisionId = Number(parts[1])
    if (params.get('topic')) payload.topic = params.get('topic')
  }
  return { tab, payload }
}

function routeHash(toTab, payload = {}) {
  if (toTab === 'decisions' && payload.decisionId) return `#/decisions/${payload.decisionId}`
  if (toTab === 'decisions' && payload.topic) return `#/decisions?topic=${encodeURIComponent(payload.topic)}`
  return `#/${TAB_IDS.has(toTab) ? toTab : 'chat'}`
}

function documentHash(docId, highlight = null) {
  const extra = highlight ? `?highlight=${encodeURIComponent(highlight)}` : ''
  return `#/documents/${docId}${extra}`
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
      className="wb-iconbtn wb-iconbtn--sm"
      onClick={toggle}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {isDark ? <Sun size={16} strokeWidth={1.8} /> : <Moon size={16} strokeWidth={1.8} />}
    </button>
  )
}

export default function App() {
  const [page, setPage] = useState('pulse')
  const [authState, setAuthState] = useState({ loading: true, needsBootstrap: false, user: null })
  const [ask, setAsk] = useState({ question: null, n: 0 })
  const [focus, setFocus] = useState({ tab: null, n: 0 })
  const [cmdkOpen, setCmdkOpen] = useState(false)
  const [docModal, setDocModal] = useState(null)
  const [resetToken, setResetToken] = useState(null)
  const [verifyToken, setVerifyToken] = useState(null)
  const [onboarding, setOnboarding] = useState(false)
  const [setup, setSetup] = useState(null)
  const [billing, setBilling] = useState(null)

  const [splitRatio, setSplitRatio] = useState(() => {
    const v = parseFloat(localStorage.getItem(SPLIT_KEY))
    return v >= SPLIT_MIN && v <= SPLIT_MAX ? v : 0.4
  })
  const [leftCollapsed, setLeftCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const [dragging, setDragging] = useState(false)
  const [isNarrow, setIsNarrow] = useState(() => window.matchMedia?.('(max-width: 820px)').matches || false)
  const splitRef = React.useRef(null)
  const lastNonDocHash = React.useRef('#/chat')

  const loadSetup = React.useCallback(() => {
    getOnboarding().then(setSetup).catch(() => setSetup(null))
  }, [])
  const loadBilling = React.useCallback(() => {
    getBillingStatus().then(setBilling).catch(() => setBilling(null))
  }, [])

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

  const applyTab = React.useCallback((tab, payload = {}) => {
    const next = mapTab(tab)
    withViewTransition(() => {
      setPage(next)
      setFocus((f) => ({ tab: next, n: f.n + 1, ...payload }))
    })
  }, [])

  useEffect(() => {
    loadAuth()
    const applyRoute = () => {
      const route = parseHashRoute()
      if (route.resetToken) { setResetToken(route.resetToken); return }
      setResetToken(null)
      if (route.verifyToken) { setVerifyToken(route.verifyToken); return }
      setVerifyToken(null)
      if (route.docModal?.docId) { setDocModal(route.docModal); return }
      const nextTab = route.tab || 'chat'
      lastNonDocHash.current = routeHash(nextTab, route.payload)
      setDocModal(null)
      applyTab(nextTab, route.payload)
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
  }, [applyTab])

  useEffect(() => {
    document.title = PAGE_DEFS[page] ? `YBase — ${PAGE_DEFS[page].label}` : 'YBase'
  }, [page])

  useEffect(() => { try { localStorage.setItem(SPLIT_KEY, String(splitRatio)) } catch { /* ignore */ } }, [splitRatio])
  useEffect(() => { try { localStorage.setItem(COLLAPSE_KEY, leftCollapsed ? '1' : '0') } catch { /* ignore */ } }, [leftCollapsed])

  useEffect(() => {
    const mq = window.matchMedia?.('(max-width: 820px)')
    if (!mq) return
    const fn = () => setIsNarrow(mq.matches)
    mq.addEventListener?.('change', fn)
    return () => mq.removeEventListener?.('change', fn)
  }, [])

  const workspaceId = authState.user?.workspace?.id
  useEffect(() => {
    if (workspaceId) { loadSetup(); loadBilling() }
    else { setSetup(null); setBilling(null) }
  }, [workspaceId, loadSetup, loadBilling])

  const userRole = authState.user?.workspace?.role

  useEffect(() => {
    const onReadonly = () => {
      loadBilling()
      // A read-only workspace can still browse memory, but write/query actions
      // should explain the block immediately instead of leaving the user on a
      // page whose controls appear to have failed silently.
      window.location.hash = '#/plans'
    }
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

  const navigate = (toTab, payload = {}) => {
    const next = routeHash(toTab, payload)
    if (window.location.hash === next) applyTab(toTab, payload)
    else window.location.hash = next
  }

  const askFromHome = (question) => setAsk((a) => ({ question, n: a.n + 1 }))

  const openDoc = (docId, highlight = null) => {
    const next = documentHash(docId, highlight)
    if (window.location.hash === next) setDocModal({ docId, highlight })
    else window.location.hash = next
  }

  const closeDoc = () => {
    setDocModal(null)
    if (window.location.hash.startsWith('#/documents/')) {
      window.history.replaceState(null, '', lastNonDocHash.current || '#/chat')
    }
  }

  const onLogout = async () => {
    try { await logout() } catch { /* already gone */ }
    setAuthState({ loading: false, needsBootstrap: false, user: null })
    navigate('home')
  }

  const startDrag = (e) => {
    e.preventDefault()
    setDragging(true)
    document.body.classList.add('is-col-resizing')
    const onMove = (ev) => {
      const rect = splitRef.current?.getBoundingClientRect()
      if (!rect) return
      const r = (ev.clientX - rect.left) / rect.width
      setSplitRatio(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, r)))
    }
    const onUp = () => {
      setDragging(false)
      document.body.classList.remove('is-col-resizing')
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
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

  if (verifyToken) {
    return (
      <ToastProvider>
        <VerifyEmail
          token={verifyToken}
          onDone={() => {
            setVerifyToken(null)
            // Re-read /me so the banner clears without a manual refresh.
            loadAuth()
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
    // The marketing page is always the public entry. On first run its CTAs open
    // the locked owner bootstrap; later they open the normal auth flow.
    const hash = window.location.hash
    const wantsAuth = /^#\/(login|signup)/.test(hash)
    const onAuthed = (user) => {
      setAuthState({ loading: false, needsBootstrap: false, user })
      navigate('home')
    }
    if (!wantsAuth) {
      return (
        <ToastProvider>
          <Marketing onEnter={(intent) => {
            window.location.hash = intent === 'signup' ? '#/signup' : '#/login'
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
  const navPages = isAdmin ? ADMIN_PAGES : MEMBER_PAGES
  // Gate admin-only pages reached via a stale hash; keep account/plans for all.
  const activePage = ADMIN_ONLY.has(page) && !isAdmin ? 'pulse' : page
  const collapsed = leftCollapsed && !isNarrow

  const onPick = (item) => {
    if (item.type === 'decision') navigate('decisions', { decisionId: item.id })
    else if (item.type === 'topic') navigate('decisions', { topic: item.title })
    else if (item.type === 'question') askFromHome(`What's the latest on “${item.title}”?`)
    else if (item.type === 'document') openDoc(item.id)
  }

  const renderPage = () => {
    switch (activePage) {
      case 'decisions':
        return <Decisions focus={focus.tab === 'decisions' ? focus : null} onOpenDoc={openDoc} onNavigate={navigate} />
      case 'sources':
        return <Sources />
      case 'settings':
        return <Settings auth={authState.user} onAuthChanged={() => { loadAuth(); loadSetup() }} />
      case 'account':
        return <Account user={authState.user} onAuthChanged={() => { loadAuth(); loadSetup(); loadBilling() }} onNavigate={navigate} onBack={() => navigate('pulse')} />
      case 'plans':
        return <Plans billing={billing} canPay={role === 'owner'} onUpgraded={() => { loadBilling(); loadAuth() }} onBack={() => navigate('pulse')} />
      default:
        return (
          <div className="page-shell">
            <h1>Ask your workspace</h1>
            <p className="settings-sub">Use the chat to search cited context from your connected sources.</p>
          </div>
        )
    }
  }

  return (
    <ToastProvider>
      <div className="wb-app">
        {authState.user?.user?.email_verified === false && (
          <VerifyBanner email={authState.user.user.email} />
        )}
        <div className="split" ref={splitRef}>
          {collapsed ? (
            <div className="left-rail">
              <button className="left-rail-btn" title="Expand panel" aria-label="Expand panel" onClick={() => setLeftCollapsed(false)}>
                <PanelLeftOpen size={18} strokeWidth={1.8} />
              </button>
              <div className="rail-pages">
                {navPages.map((id) => {
                  const { label, Icon } = PAGE_DEFS[id]
                  return (
                    <button
                      key={id}
                      className={`left-rail-btn${activePage === id ? ' is-active' : ''}`}
                      title={label}
                      aria-label={label}
                      onClick={() => { setLeftCollapsed(false); navigate(id) }}
                    >
                      <Icon size={18} strokeWidth={1.8} />
                    </button>
                  )
                })}
              </div>
            </div>
          ) : (
            <section className="split-left" style={isNarrow ? undefined : { width: `${splitRatio * 100}%` }}>
              <div className="panel-head">
                <AccountMenu
                  workspace={workspace}
                  user={authState.user}
                  role={role}
                  isAdmin={isAdmin}
                  onNavigate={navigate}
                  onSearch={() => setCmdkOpen(true)}
                  onLogout={onLogout}
                />
                <div className="panel-head-actions">
                  <ThemeToggle />
                  {!isNarrow && (
                    <button className="wb-iconbtn wb-iconbtn--sm" title="Collapse panel" aria-label="Collapse panel" onClick={() => setLeftCollapsed(true)}>
                      <PanelLeftClose size={16} strokeWidth={1.8} />
                    </button>
                  )}
                </div>
              </div>
              <nav className="leftnav" aria-label="Workspace pages">
                {navPages.map((id) => {
                  const { label, Icon } = PAGE_DEFS[id]
                  return (
                    <button
                      key={id}
                      className={`leftnav-btn${activePage === id ? ' is-active' : ''}`}
                      onClick={() => navigate(id)}
                    >
                      <Icon size={15} strokeWidth={1.9} /> {label}
                    </button>
                  )
                })}
              </nav>
              <div className="left-scroll">{renderPage()}</div>
            </section>
          )}

          {!collapsed && !isNarrow && (
            <div
              className={`split-divider${dragging ? ' is-dragging' : ''}`}
              onPointerDown={startDrag}
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize panels"
            />
          )}

          <section className="split-right">
            <Chat
              pendingAsk={ask}
              canAdd={isAdmin}
              onAddDoc={() => navigate('sources')}
              onOpenDoc={openDoc}
              onNavigate={navigate}
              onToggleFull={() => setLeftCollapsed((c) => !c)}
              fullChat={collapsed}
            />
          </section>
        </div>

        <CmdK open={cmdkOpen} onClose={() => setCmdkOpen(false)} onPick={onPick} />
        {docModal && (
          <DocModal docId={docModal.docId} highlight={docModal.highlight} onClose={closeDoc} />
        )}
      </div>
    </ToastProvider>
  )
}
