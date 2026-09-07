import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 10000,
})

http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    const msg = err.response?.data?.detail || err.message || '请求失败'
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
}
