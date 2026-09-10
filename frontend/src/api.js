import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 10000,
})

// —— token 持久化（localStorage，持久登录）——
const TOKEN_KEY = 'auth_token'
const USER_KEY = 'auth_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}
export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}
export function getStoredUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY))
  } catch {
    return null
  }
}

// 请求拦截：自动带 Authorization
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    // 后端熔断返回 402，detail 为对象（含 code='cost_limit'、approval_token 等）
    const detail = err.response?.data?.detail
    if (err.response?.status === 402 && detail && typeof detail === 'object') {
      const e = new Error(detail.message || '调用超限，需确认')
      e.status = 402
      e.costDetail = detail
      return Promise.reject(e)
    }
    // 401：登录失效，清除本地态并跳登录（避免无限循环）
    if (err.response?.status === 401) {
      clearAuth()
      if (window.location.pathname !== '/login') {
        // 交给 App.vue 的登录态监听处理跳转
        window.dispatchEvent(new Event('auth-expired'))
      }
    }
    const msg = typeof detail === 'string' ? detail : err.message || '请求失败'
    return Promise.reject(new Error(msg))
  },
)

export const authApi = {
  register(username, password) {
    return http.post('/auth/register', { username, password })
  },
  login(username, password) {
    return http.post('/auth/login', { username, password })
  },
  me() {
    return http.get('/auth/me')
  },
  changePassword(oldPassword, newPassword) {
    return http.post('/auth/change-password', {
      old_password: oldPassword,
      new_password: newPassword,
    })
  },
}

export const kbApi = {
  listDocuments(page = 1, pageSize = 20) {
    return http.get('/kb/documents', { params: { page, page_size: pageSize } })
  },
  uploadDocument(file, category) {
    const form = new FormData()
    form.append('file', file)
    if (category) form.append('category', category)
    return http.post('/kb/documents', form)
  },
  deleteDocument(path) {
    return http.delete('/kb/documents', { params: { path } })
  },
  rebuild() {
    return http.post('/kb/rebuild')
  },
  rebuildStatus() {
    return http.get('/kb/rebuild/status')
  },
}

export const competitorApi = {
  list(params) {
    return http.get('/competitors', { params })
  },
  get(id) {
    return http.get(`/competitors/${id}`)
  },
  create(payload) {
    return http.post('/competitors', payload)
  },
  update(id, payload) {
    return http.put(`/competitors/${id}`, payload)
  },
  remove(id) {
    return http.delete(`/competitors/${id}`)
  },
  // 抓取并提炼（落库为待校对）
  fetch(url) {
    return http.post('/fetch', { url })
  },
  // 校对通过：待校对 → 已入库
  approve(id) {
    return http.post(`/fetch/${id}/approve`)
  },
  // 多竞品对比
  compare(ids) {
    return http.post('/compare', { ids })
  },
  // —— 设置：评分维度 ——
  getRatingDimensions() {
    return http.get('/settings/rating-dimensions')
  },
  setRatingDimensions(dimensions) {
    return http.put('/settings/rating-dimensions', { dimensions })
  },
  // —— 报告 ——
  exportReport(ids) {
    return http.post('/report/export', ids && ids.length ? { ids } : {})
  },
  reportDownloadUrl(filename) {
    return `/api/report/download/${encodeURIComponent(filename)}`
  },
  // —— LLM 成本熔断 ——
  costStatus() {
    return http.get('/cost/status')
  },
  costApprove(token) {
    return http.post('/cost/approve', { token })
  },
  costReset() {
    return http.post('/cost/reset')
  },
  // —— RAG 知识库问答 ——
  ragAsk(question, topK = 5, sessionId = null) {
    const payload = { question, top_k: topK }
    if (sessionId) payload.session_id = sessionId
    return http.post('/rag/ask', payload)
  },
  // 流式问答：返回 async 生成器，逐次 yield {type:'delta',content} | {type:'done',...}
  // 用原生 fetch 读 ReadableStream，绕开 axios 不支持流式响应的问题
  async *ragAskStream(question, topK = 5, sessionId = null) {
    const payload = { question, top_k: topK }
    if (sessionId) payload.session_id = sessionId
    const token = getToken()
    const resp = await fetch('/api/rag/ask/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      if (resp.status === 401) {
        clearAuth()
        window.dispatchEvent(new Event('auth-expired'))
      }
      let msg = '请求失败'
      try {
        const d = await resp.json()
        msg = typeof d.detail === 'string' ? d.detail : msg
      } catch {
        /* ignore */
      }
      throw new Error(msg)
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // SSE 事件按 \n\n 分隔
      let idx
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const line = raw.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        const data = line.slice(5).trim()
        if (data === '[DONE]') return
        try {
          yield JSON.parse(data)
        } catch {
          /* 忽略无法解析的行 */
        }
      }
    }
  },
  ragStatus() {
    return http.get('/rag/status')
  },
  // 多轮会话
  ragCreateSession() {
    return http.post('/rag/sessions')
  },
  ragListSessions() {
    return http.get('/rag/sessions')
  },
  ragGetSession(sessionId) {
    return http.get(`/rag/sessions/${sessionId}`)
  },
  ragBatchDeleteSessions(sessionIds) {
    return http.post('/rag/sessions/batch-delete', { session_ids: sessionIds })
  },
  ragDeleteSession(sessionId) {
    return http.delete(`/rag/sessions/${sessionId}`)
  },
  // 问答反馈（👍/👎）
  ragFeedback(payload) {
    return http.post('/rag/feedback', payload)
  },
  // —— 竞品变化记录 ——
  listChanges(params) {
    return http.get('/changes', { params })
  },
  triggerMonitor() {
    return http.post('/changes/monitor')
  },
}
