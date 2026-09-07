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
}
