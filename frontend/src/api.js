export async function getJSON(path) {
  const res = await fetch(path, { credentials: 'include' })
  if (res.status === 401) window.dispatchEvent(new Event('auth:required'))
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export async function postJSON(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (res.status === 401) window.dispatchEvent(new Event('auth:required'))
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export async function deleteJSON(path) {
  const res = await fetch(path, { method: 'DELETE', credentials: 'include' })
  if (res.status === 401) window.dispatchEvent(new Event('auth:required'))
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export async function patchJSON(path, body) {
  const res = await fetch(path, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (res.status === 401) window.dispatchEvent(new Event('auth:required'))
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const listSessions = () => getJSON('/api/sessions')
export const createSession = (title) => postJSON('/api/sessions', { title })
export const getSession = (id) => getJSON(`/api/sessions/${id}`)
export const saveMessage = (sessionId, role, content, meta = null) =>
  postJSON(`/api/sessions/${sessionId}/messages`, { role, content, meta })
export const deleteSession = (id) => deleteJSON(`/api/sessions/${id}`)

export const getDecisionShare = (nodeId) => getJSON(`/api/decisions/${nodeId}/share`)
export const createDecisionShare = (nodeId) => postJSON(`/api/decisions/${nodeId}/share`, {})
export const revokeDecisionShare = (nodeId) => deleteJSON(`/api/decisions/${nodeId}/share`)
export const getSharedDecision = (token) =>
  getJSON(`/api/shared/decisions/${encodeURIComponent(token)}`)

export const searchMemory = (q) => getJSON(`/api/search?q=${encodeURIComponent(q)}`)
export const getNode = (id) => getJSON(`/api/nodes/${id}`)
export const getDocument = (id, full = false) =>
  getJSON(`/api/documents/${id}${full ? '?full=true' : ''}`)
export const listPeople = () => getJSON('/api/people')
export const getPerson = (id) => getJSON(`/api/people/${id}`)
export const getStats = (since = null) =>
  getJSON(`/api/stats${since ? `?since=${encodeURIComponent(since)}` : ''}`)
export const getHealth = () => getJSON('/api/health')
export const getHealthDetails = () => getJSON('/api/health/details')
export const getOpsOverview = () => getJSON('/api/ops/overview')
export const getAnalyticsOverview = (days = 30) =>
  getJSON(`/api/analytics/overview?days=${days}`)
export const getMemoryQuality = () => getJSON('/api/analytics/quality')
export const retryFailedDocuments = () => postJSON('/api/ops/failed-documents/retry', {})
export const seedDemoData = () => postJSON('/api/ops/demo-seed', {})

export const listDigests = () => getJSON('/api/digests')
export const getLatestDigest = () => getJSON('/api/digests/latest')
export const runDigest = () => postJSON('/api/digests/run', {})

export const getBootstrapStatus = () => getJSON('/api/auth/bootstrap-status')
export const getAuthProviders = () => getJSON('/api/auth/providers')
export const bootstrap = (body) => postJSON('/api/auth/bootstrap', body)
export const register = (body) => postJSON('/api/auth/register', body)
export const login = (body) => postJSON('/api/auth/login', body)
export const logout = () => postJSON('/api/auth/logout', {})
export const getMe = () => getJSON('/api/auth/me')
export const updateMe = (body) => patchJSON('/api/auth/me', body)
export const forgotPassword = (email) => postJSON('/api/auth/forgot', { email })
export const resetPassword = (token, new_password) =>
  postJSON('/api/auth/reset', { token, new_password })
export const switchWorkspace = (workspaceId) =>
  postJSON('/api/auth/switch-workspace', { workspace_id: workspaceId })
export const getInvite = (token) => getJSON(`/api/auth/invite/${encodeURIComponent(token)}`)
export const joinWorkspace = (body) => postJSON('/api/auth/join', body)
export const createWorkspace = (name) => postJSON('/api/workspace/create', { name })
export const getOnboarding = () => getJSON('/api/workspace/onboarding')
export const completeOnboarding = () => postJSON('/api/workspace/onboarding/complete', {})
export const transferOwnership = (userId) =>
  postJSON('/api/workspace/transfer-ownership', { new_owner_user_id: userId })
export const listWorkspaceUsers = () => getJSON('/api/workspace/users')
export const createWorkspaceUser = (body) => postJSON('/api/workspace/users', body)
export const patchWorkspaceUser = (id, body) => patchJSON(`/api/workspace/users/${id}`, body)
export const listWorkspaceInvites = () => getJSON('/api/workspace/invites')
export const createWorkspaceInvite = (body) => postJSON('/api/workspace/invites', body)
export const revokeWorkspaceInvite = (id) => deleteJSON(`/api/workspace/invites/${id}`)

export const listSources = () => getJSON('/api/sources')
export const getSlackInstallUrl = () => getJSON('/api/sources/slack/install-url')
export const getJiraInstallUrl = () => getJSON('/api/sources/jira/install-url')
export const getGitHubInstallUrl = () => getJSON('/api/sources/github/install-url')
export const listSourceStreams = (connectionId) =>
  getJSON(`/api/sources/${connectionId}/streams`)
export const patchSourceStream = (connectionId, streamId, body) =>
  patchJSON(`/api/sources/${connectionId}/streams/${streamId}`, body)
export const startSourceSync = (connectionId, days = 90) =>
  postJSON(`/api/sources/${connectionId}/sync`, { days })
export const listSourceJobs = (connectionId) =>
  getJSON(`/api/sources/${connectionId}/jobs`)
export const retrySourceJob = (connectionId, jobId) =>
  postJSON(`/api/sources/${connectionId}/jobs/${jobId}/retry`, {})
export const deleteSource = (connectionId) => deleteJSON(`/api/sources/${connectionId}`)

export const listReviewNodes = ({ state = 'needs_review', kind = '', q = '' } = {}) => {
  const params = new URLSearchParams()
  if (state) params.set('state', state)
  if (kind) params.set('kind', kind)
  if (q) params.set('q', q)
  return getJSON(`/api/memory-review?${params.toString()}`)
}
export const getReviewNode = (id) => getJSON(`/api/memory-review/${id}`)
export const patchReviewNode = (id, body) => patchJSON(`/api/memory-review/${id}`, body)
export const archiveReviewNode = (id, reason = '') =>
  postJSON(`/api/memory-review/${id}/archive`, { reason })
export const unarchiveReviewNode = (id) => postJSON(`/api/memory-review/${id}/unarchive`, {})

export const submitAnswerFeedback = (body) => postJSON('/api/answer-feedback', body)
export const getMyAnswerFeedback = (chatMessageId) =>
  getJSON(`/api/answer-feedback/mine?chat_message_id=${encodeURIComponent(chatMessageId)}`)
export const listAnswerFeedback = ({ status = 'open', issue_type = '' } = {}) => {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (issue_type) params.set('issue_type', issue_type)
  return getJSON(`/api/answer-feedback?${params.toString()}`)
}
export const getAnswerFeedback = (id) => getJSON(`/api/answer-feedback/${id}`)
export const patchAnswerFeedback = (id, body) => patchJSON(`/api/answer-feedback/${id}`, body)

// POST /api/query and dispatch SSE events to handlers:
// { status, delta, metadata, error, done }
// `history` carries the prior turns so follow-up questions work.
export async function streamQuery(question, handlers, history = []) {
  const res = await fetch('/api/query', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, history }),
  })
  if (res.status === 401) window.dispatchEvent(new Event('auth:required'))
  if (!res.ok || !res.body) throw new Error(`query failed: ${res.status}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      let event = 'message'
      let data = ''
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (data && handlers[event]) {
        try { handlers[event](JSON.parse(data)) } catch { /* skip bad frame */ }
      }
    }
  }
}
