import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 10000,
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
    const msg = typeof detail === 'string' ? detail : err.message || '请求失败'
    return Promise.reject(new Error(msg))
  },
)

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
}
