<script setup>
import { ref, onMounted, watch, nextTick, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// echarts 按需引入：只注册用到的雷达图组件，避免全量打包（chunk 从 ~2.2MB 降到 ~500KB）
import * as echarts from 'echarts/core'
import { RadarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { competitorApi, authApi, kbApi, getToken, setAuth, clearAuth, getStoredUser } from './api'

echarts.use([RadarChart, TooltipComponent, LegendComponent, CanvasRenderer])

// —— 登录态 ——
const currentUser = ref(getStoredUser())
const isLoggedIn = computed(() => !!currentUser.value)
const isAdmin = computed(() => currentUser.value?.role === 'admin')

// 登录/注册表单
const authMode = ref('login') // 'login' | 'register'
const authUsername = ref('')
const authPassword = ref('')
const authConfirm = ref('')
const authLoading = ref(false)

function enterApp(token, user) {
  setAuth(token, user)
  currentUser.value = user
  // 登录后初始化数据
  fetchList()
  fetchPending()
  loadDims()
  loadCostStatus()
  loadRagStatus()
  loadRagSessions()
  loadChanges()
  if (isAdmin.value) loadKbDocuments()
}

async function doLogin() {
  if (!authUsername.value.trim() || !authPassword.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  authLoading.value = true
  try {
    const r = await authApi.login(authUsername.value.trim(), authPassword.value)
    enterApp(r.access_token, r.user)
    ElMessage.success('登录成功')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    authLoading.value = false
  }
}

async function doRegister() {
  if (!authUsername.value.trim() || authPassword.value.length < 6) {
    ElMessage.warning('用户名至少 3 位，密码至少 6 位')
    return
  }
  if (authPassword.value !== authConfirm.value) {
    ElMessage.warning('两次密码不一致')
    return
  }
  authLoading.value = true
  try {
    const r = await authApi.register(authUsername.value.trim(), authPassword.value)
    enterApp(r.access_token, r.user)
    ElMessage.success('注册成功，已自动登录')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    authLoading.value = false
  }
}

function switchAuthMode(mode) {
  authMode.value = mode
  authConfirm.value = ''
}

function doLogout() {
  clearAuth()
  currentUser.value = null
  activeTab.value = 'list'
  // 清空会话等敏感本地态
  ragSessionId.value = null
  ragHistory.value = []
  ragSessions.value = []
  ElMessage.success('已退出登录')
}

// 改密对话框
const pwdDialog = ref(false)
const pwdOld = ref('')
const pwdNew = ref('')
const pwdConfirm = ref('')
const pwdSaving = ref(false)

function openChangePwd() {
  pwdOld.value = ''
  pwdNew.value = ''
  pwdConfirm.value = ''
  pwdDialog.value = true
}

async function doChangePwd() {
  if (pwdNew.value.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  if (pwdNew.value !== pwdConfirm.value) {
    ElMessage.warning('两次新密码不一致')
    return
  }
  pwdSaving.value = true
  try {
    await authApi.changePassword(pwdOld.value, pwdNew.value)
    ElMessage.success('密码已修改，下次登录请使用新密码')
    pwdDialog.value = false
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    pwdSaving.value = false
  }
}

// 登录态失效监听（401 触发）
onMounted(() => {
  window.addEventListener('auth-expired', () => {
    currentUser.value = null
    ElMessage.warning('登录已过期，请重新登录')
  })
  if (isLoggedIn.value) {
    // 已有 token，直接初始化
    fetchList()
    fetchPending()
    loadDims()
    loadCostStatus()
    loadRagStatus()
    loadRagSessions()
    loadChanges()
    if (isAdmin.value) loadKbDocuments()
  }
})

const activeTab = ref('list')

// —— 左侧导航菜单 ——
// icon 用 emoji 占位，落地后如需可换 element-plus icon；label 与 name 对应 activeTab
const menuItems = [
  { name: 'list', label: '竞品列表', icon: '📊' },
  { name: 'pending', label: '待校对', icon: '📝' },
  { name: 'form', label: '录入竞品', icon: '➕' },
  { name: 'compare', label: '对比', icon: '⚖️' },
  { name: 'rag', label: '知识库问答', icon: '💬' },
  { name: 'changes', label: '变化记录', icon: '🔔' },
  { name: 'kb', label: '知识库管理', icon: '📚', adminOnly: true },
  { name: 'settings', label: '设置', icon: '⚙️' },
]
// 可见菜单：普通用户过滤掉 adminOnly
const visibleMenu = computed(() =>
  menuItems.filter((m) => !m.adminOnly || isAdmin.value),
)
// 当前页面的标题/副标题（顶部面包屑用）
const pageMeta = {
  list: ['竞品列表', '竞品情报收集、整理与横向对比'],
  pending: ['待校对', 'AI 提炼结果人工审核'],
  form: ['录入竞品', '手动新增竞品信息'],
  compare: ['多竞品对比', '评分维度横向雷达对比'],
  rag: ['知识库问答', '融合 RAG 的智能问答助手'],
  changes: ['变化记录', '竞品信息变更历史追踪'],
  kb: ['知识库管理', '知识库语料维护（管理员专属）'],
  settings: ['设置', '评分维度与系统配置'],
}
const currentTitle = computed(() => pageMeta[activeTab.value]?.[0] || '')
const currentSub = computed(() => pageMeta[activeTab.value]?.[1] || '')

const competitors = ref([])
const loading = ref(false)
const keyword = ref('')
const filterType = ref('')
// 竞品列表前端分页
const compPage = ref(1)
const compPageSize = 10

// 统计卡片数据（从已加载列表 + 待校对推导）
const compStats = computed(() => {
  const total = competitors.value.length
  const online = competitors.value.filter((c) => c.launch_status === '已上线').length
  return {
    total,
    online,
    offline: total - online,
    pending: pending.value.length,
  }
})
// 当前页竞品（前端分页）
const pagedCompetitors = computed(() => {
  const start = (compPage.value - 1) * compPageSize
  return competitors.value.slice(start, start + compPageSize)
})

// —— 抓取 ——
const fetchUrl = ref('')
const fetching = ref(false)
const fetchDialogVisible = ref(false)

// —— 待校对 ——
const pending = ref([])
const pendingLoading = ref(false)

// —— 对比 ——
const selectedIds = ref([])
const compareData = ref(null)
const compareLoading = ref(false)
const radarEl = ref(null)
let radarChart = null

const typeOptions = ['C2C 闲置交易', '回收置换', '质检转卖']
const statusOptions = ['已上线', '内测', '已下线']
// 评分维度：从后端设置读取（可在"设置"页配置）
const DEFAULT_DIMS = ['功能完整度', '用户规模', '质检能力', '价格竞争力', '口碑']
const ratingDims = ref([...DEFAULT_DIMS])

// —— 列表 ——
async function fetchList() {
  loading.value = true
  try {
    const params = {}
    if (keyword.value) params.keyword = keyword.value
    if (filterType.value) params.type = filterType.value
    competitors.value = await competitorApi.list(params)
    compPage.value = 1
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function onSearch() {
  fetchList()
}

// —— 录入 ——
const form = ref(emptyForm())
const saving = ref(false)
const isEdit = ref(false)
const editId = ref(null)

function emptyRatings() {
  const r = {}
  ratingDims.value.forEach((d) => {
    r[d] = 0
  })
  return r
}

function emptyForm() {
  return {
    name: '',
    type: 'C2C 闲置交易',
    positioning: '',
    website: '',
    launch_status: '已上线',
    features: '',
    target_users: '',
    business_model: '',
    pricing: '',
    strengths: '',
    weaknesses: '',
    tags: [],
    remark: '',
    ratings: emptyRatings(),
  }
}

function openCreate() {
  form.value = emptyForm()
  isEdit.value = false
  editId.value = null
  activeTab.value = 'form'
}

function openEdit(row) {
  form.value = {
    name: row.name,
    type: row.type,
    positioning: row.positioning || '',
    website: row.website || '',
    launch_status: row.launch_status || '已上线',
    features: row.features || '',
    target_users: row.target_users || '',
    business_model: row.business_model || '',
    pricing: row.pricing || '',
    strengths: row.strengths || '',
    weaknesses: row.weaknesses || '',
    tags: row.tags || [],
    remark: row.remark || '',
    ratings: { ...emptyRatings(), ...(row.ext_fields?.ratings || {}) },
  }
  isEdit.value = true
  editId.value = row.id
  activeTab.value = 'form'
}

async function save() {
  if (!form.value.name.trim()) {
    ElMessage.warning('名称不能为空')
    return
  }
  saving.value = true
  try {
    // 评分写入 ext_fields.ratings，其余字段平铺
    const payload = { ...form.value }
    payload.ext_fields = { ratings: form.value.ratings }
    delete payload.ratings
    if (isEdit.value) {
      await competitorApi.update(editId.value, payload)
      ElMessage.success('已更新')
    } else {
      await competitorApi.create(payload)
      ElMessage.success('已录入')
    }
    activeTab.value = 'list'
    fetchList()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

function cancel() {
  activeTab.value = 'list'
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.name}」？`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await competitorApi.remove(row.id)
    ElMessage.success('已删除')
    fetchList()
    fetchPending()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// —— 抓取 ——
function openFetch() {
  fetchUrl.value = ''
  fetchDialogVisible.value = true
}

// —— 成本熔断征询 ——
const costDialog = ref(false)
const costInfo = ref(null) // 待审批信息 { approval_token, estimate_cost_cny, total_cost_cny, cost_limit_cny }
const costStatus = ref(null) // 累计状态
const costConfirming = ref(false)
let pendingFetchUrl = '' // 用户同意后要重试的 URL

async function loadCostStatus() {
  try {
    costStatus.value = await competitorApi.costStatus()
  } catch (e) {
    /* 非关键，静默 */
  }
}

function openCostApprove(info, url) {
  costInfo.value = info
  pendingFetchUrl = url
  costDialog.value = true
}

async function confirmFetch() {
  costConfirming.value = true
  try {
    await competitorApi.costApprove(costInfo.value.approval_token)
    costDialog.value = false
    // 放行后重试本次抓取
    await doFetchUrl(pendingFetchUrl)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    costConfirming.value = false
  }
}

function cancelFetch() {
  costDialog.value = false
  pendingFetchUrl = ''
}

async function resetCost() {
  try {
    await competitorApi.costReset()
    await loadCostStatus()
    ElMessage.success('已重置成本累计')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function doFetchUrl(url) {
  try {
    await competitorApi.fetch(url)
    ElMessage.success('抓取成功，已进入待校对')
    fetchDialogVisible.value = false
    fetchPending()
    loadCostStatus()
  } catch (e) {
    if (e.status === 402 && e.costDetail) {
      // 成本熔断：弹框征询是否同意继续
      openCostApprove(e.costDetail, url)
      loadCostStatus()
    } else {
      ElMessage.error(e.message)
    }
  }
}

async function doFetch() {
  const url = fetchUrl.value.trim()
  if (!url) {
    ElMessage.warning('请输入目标 URL')
    return
  }
  fetching.value = true
  try {
    await doFetchUrl(url)
  } finally {
    fetching.value = false
  }
}

// —— 待校对 ——
async function fetchPending() {
  pendingLoading.value = true
  try {
    pending.value = await competitorApi.list({ data_status: '待校对' })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    pendingLoading.value = false
  }
}

async function approve(row) {
  try {
    await competitorApi.approve(row.id)
    ElMessage.success('已确认入库')
    fetchPending()
    fetchList()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function dataStatusTag(s) {
  const map = { draft: 'info', 待校对: 'warning', 已入库: 'success' }
  return map[s] || 'info'
}

function typeTag(type) {
  const map = { 'C2C 闲置交易': 'primary', '回收置换': 'warning', '质检转卖': 'success' }
  return map[type] || 'info'
}

function statusTag(s) {
  const map = { 已上线: 'success', 内测: 'warning', 已下线: 'info' }
  return map[s] || 'info'
}

// —— 对比 ——
function onSelectionChange(rows) {
  selectedIds.value = rows.map((r) => r.id)
}

function goCompare() {
  if (selectedIds.value.length < 2) {
    ElMessage.warning('请至少勾选 2 个竞品进行对比')
    return
  }
  activeTab.value = 'compare'
  loadCompare()
}

async function loadCompare() {
  if (selectedIds.value.length < 2) return
  compareLoading.value = true
  try {
    compareData.value = await competitorApi.compare(selectedIds.value)
    await nextTick()
    renderRadar()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    compareLoading.value = false
  }
}

function renderRadar() {
  if (!compareData.value || !radarEl.value) return
  if (radarChart) radarChart.dispose()
  radarChart = echarts.init(radarEl.value)
  const dims = compareData.value.dimensions
  const palette = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272']
  const series = compareData.value.items.map((item, i) => ({
    value: dims.map((d) => item.ratings[d] ?? 0),
    name: item.name,
    lineStyle: { color: palette[i % palette.length] },
    itemStyle: { color: palette[i % palette.length] },
    areaStyle: { opacity: 0.08 },
  }))
  radarChart.setOption({
    tooltip: {},
    legend: { data: compareData.value.items.map((i) => i.name), bottom: 0 },
    radar: {
      indicator: dims.map((d) => ({ name: d, max: 5 })),
      radius: '65%',
    },
    series: [{ type: 'radar', data: series }],
  })
}

// —— 报告导出 ——
const exporting = ref(false)

async function doExportReport() {
  exporting.value = true
  try {
    // 勾选了竞品则只导出勾选项，否则导出全部已入库竞品
    const ids = selectedIds.value.length >= 1 ? selectedIds.value : null
    const r = await competitorApi.exportReport(ids)
    window.open(competitorApi.reportDownloadUrl(r.filename), '_blank')
    ElMessage.success(`报告已生成（${r.competitor_count} 个竞品）`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    exporting.value = false
  }
}

// —— 设置：评分维度 ——
const dimsDraft = ref([])
const newDim = ref('')
const dimsSaving = ref(false)

async function loadDims() {
  try {
    const r = await competitorApi.getRatingDimensions()
    ratingDims.value = r.dimensions
    dimsDraft.value = [...r.dimensions]
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function addDim() {
  const d = newDim.value.trim()
  if (!d) return
  if (dimsDraft.value.includes(d)) {
    ElMessage.warning('该维度已存在')
    return
  }
  if (dimsDraft.value.length >= 8) {
    ElMessage.warning('最多 8 个维度（雷达图过多难以阅读）')
    return
  }
  dimsDraft.value.push(d)
  newDim.value = ''
}

function removeDim(d) {
  if (dimsDraft.value.length <= 3) {
    ElMessage.warning('至少保留 3 个维度')
    return
  }
  dimsDraft.value = dimsDraft.value.filter((x) => x !== d)
}

async function saveDims() {
  if (dimsDraft.value.length < 3) {
    ElMessage.warning('至少 3 个维度')
    return
  }
  dimsSaving.value = true
  try {
    const r = await competitorApi.setRatingDimensions(dimsDraft.value)
    ratingDims.value = r.dimensions
    ElMessage.success('评分维度已更新（新维度对雷达图、对比与报告立即生效）')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    dimsSaving.value = false
  }
}

// —— RAG 知识库问答 ——
const ragQuestion = ref('')
const ragAnswer = ref(null) // { answer, mode, sources }
const ragLoading = ref(false)
const ragReady = ref(null) // null=未知, true/false
const ragSessionId = ref(null) // 当前会话 ID（多轮记忆）
const ragHistory = ref([]) // 本轮会话的问答历史 [{ role, content }]
const ragSessions = ref([]) // 历史会话列表

async function loadRagStatus() {
  try {
    const r = await competitorApi.ragStatus()
    ragReady.value = r.ready
  } catch (e) {
    ragReady.value = false
  }
}

async function loadRagSessions() {
  try {
    ragSessions.value = await competitorApi.ragListSessions()
  } catch (e) {
    ragSessions.value = []
  }
}

function newRagSession() {
  // 新对话：仅本地清空（不再立即落库）。首问时前端生成 session_id，
  // 后端 ask 懒创建会话，避免侧栏堆积「空会话」。
  ragSessionId.value = null
  ragHistory.value = []
  ragAnswer.value = null
  batchMode.value = false
  checkedSessions.value = []
}

async function doAsk() {
  const q = ragQuestion.value.trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  ragLoading.value = true
  try {
    // 新对话的首问：本地生成 session_id，后端 ask 懒创建并归属当前用户
    if (!ragSessionId.value) {
      ragSessionId.value = crypto.randomUUID().replace(/-/g, '')
    }
    const resp = await competitorApi.ragAsk(q, 5, ragSessionId.value)
    ragAnswer.value = resp
    // 记录本轮问答到本地历史（AI 消息带上来源引用）
    ragHistory.value.push({ role: 'user', content: q })
    ragHistory.value.push({
      role: 'assistant',
      content: resp.answer,
      sources: resp.sources || [],
      mode: resp.mode,
    })
    ragQuestion.value = ''
    loadRagSessions()
    scrollChatToBottom()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    ragLoading.value = false
  }
}

// 聊天区自动滚动到底部
const chatMsgsEl = ref(null)
function scrollChatToBottom() {
  nextTick(() => {
    if (chatMsgsEl.value) chatMsgsEl.value.scrollTop = chatMsgsEl.value.scrollHeight
  })
}

async function copySourceText(s) {
  // 复制来源文本，供用户溯源核对
  try {
    await navigator.clipboard.writeText(`【${s.doc_id}】${s.title}\n${s.text}`)
    ElMessage.success('来源文本已复制')
  } catch (e) {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

// —— 竞品变化记录 ——
const changes = ref([])
const changesLoading = ref(false)

async function loadChanges() {
  changesLoading.value = true
  try {
    changes.value = await competitorApi.listChanges({ limit: 100 })
  } catch (e) {
    ElMessage.error('加载变化记录失败：' + e.message)
  } finally {
    changesLoading.value = false
  }
}

async function doMonitor() {
  try {
    const r = await competitorApi.triggerMonitor()
    ElMessage.success(`监控检查完成：共检查 ${r.checked} 个竞品`)
    loadChanges()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// —— 知识库管理（admin 专属） ——
const kbDocuments = ref([])
const kbLoading = ref(false)
const kbRebuilding = ref(false)
const kbUploadCategory = ref('')
const kbUploadFile = ref(null)
// 分页
const kbPage = ref(1)
const kbPageSize = 20
const kbTotal = ref(0)

async function loadKbDocuments() {
  kbLoading.value = true
  try {
    const r = await kbApi.listDocuments(kbPage.value, kbPageSize)
    kbDocuments.value = r.items
    kbTotal.value = r.total
  } catch (e) {
    ElMessage.error('加载知识库文档失败：' + e.message)
  } finally {
    kbLoading.value = false
  }
}

function onKbPageChange(p) {
  kbPage.value = p
  loadKbDocuments()
}

function onKbFileChange(file) {
  kbUploadFile.value = file.raw
}

async function doKbUpload() {
  if (!kbUploadFile.value) {
    ElMessage.warning('请先选择 .md 文件')
    return
  }
  try {
    await kbApi.uploadDocument(kbUploadFile.value, kbUploadCategory.value || undefined)
    ElMessage.success('上传成功，请重建向量库后生效')
    kbUploadFile.value = null
    loadKbDocuments()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function doKbDelete(doc) {
  try {
    await ElMessageBox.confirm(`确认删除「${doc.filename}」？删除后需重建向量库才彻底移除。`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await kbApi.deleteDocument(doc.path)
    ElMessage.success('已删除')
    loadKbDocuments()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function doKbRebuild() {
  try {
    await ElMessageBox.confirm(
      '重建向量库会重新分块并 embedding 全部语料（首次可能耗时较长）。确认继续？',
      '重建确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  kbRebuilding.value = true
  try {
    await kbApi.rebuild()
    ElMessage.success('已触发重建，正在后台执行…')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    kbRebuilding.value = false
  }
}

function kbSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

// —— RAG 会话侧栏 ——
// 会话时间显示：今天显示 HH:mm，否则显示 MM-DD
function fmtSessionTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  if (d.toDateString() === now.toDateString()) return hm
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

async function switchSession(sessionId) {
  ragSessionId.value = sessionId
  ragHistory.value = []
  ragAnswer.value = null
  try {
    // 从后端拉取该会话的完整消息历史并回显到对话区
    const detail = await competitorApi.ragGetSession(sessionId)
    ragHistory.value = (detail.messages || []).map((m) => ({
      role: m.role,
      content: m.content,
    }))
    scrollChatToBottom()
  } catch (e) {
    ElMessage.error('加载会话失败：' + e.message)
  }
}

// —— 批量删除 ——
const batchMode = ref(false) // 批量删除模式
const checkedSessions = ref([]) // 勾选的 session_id

function toggleBatchMode() {
  batchMode.value = !batchMode.value
  checkedSessions.value = []
}

function toggleSessionCheck(sessionId) {
  const i = checkedSessions.value.indexOf(sessionId)
  if (i >= 0) {
    checkedSessions.value.splice(i, 1)
  } else {
    checkedSessions.value.push(sessionId)
  }
}

const allChecked = computed(
  () => ragSessions.value.length > 0 && checkedSessions.value.length === ragSessions.value.length
)

function toggleCheckAll() {
  checkedSessions.value = allChecked.value ? [] : ragSessions.value.map((s) => s.session_id)
}

async function doBatchDelete() {
  if (!checkedSessions.value.length) {
    ElMessage.warning('请先勾选要删除的会话')
    return
  }
  try {
    await ElMessageBox.confirm(`确认删除选中的 ${checkedSessions.value.length} 个会话？`, '批量删除', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const r = await competitorApi.ragBatchDeleteSessions([...checkedSessions.value])
    ElMessage.success(`已删除 ${r.deleted} 个会话`)
    // 若当前会话被删除，清空对话区
    if (checkedSessions.value.includes(ragSessionId.value)) {
      ragSessionId.value = null
      ragHistory.value = []
      ragAnswer.value = null
    }
    checkedSessions.value = []
    batchMode.value = false
    loadRagSessions()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <!-- 未登录：登录/注册视图 -->
  <div v-if="!isLoggedIn" class="auth-wrap">
    <div class="auth-card">
      <div class="auth-brand">
        <h1>二手交易平台分析系统</h1>
        <p class="sub">竞品情报 · 知识库问答</p>
      </div>
      <el-form @submit.prevent>
        <el-form-item>
          <el-input v-model="authUsername" placeholder="用户名" size="large" clearable />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="authPassword"
            type="password"
            placeholder="密码"
            size="large"
            show-password
            @keyup.enter="authMode === 'login' ? doLogin() : doRegister()"
          />
        </el-form-item>
        <el-form-item v-if="authMode === 'register'">
          <el-input
            v-model="authConfirm"
            type="password"
            placeholder="确认密码"
            size="large"
            show-password
            @keyup.enter="doRegister"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          class="auth-submit"
          :loading="authLoading"
          @click="authMode === 'login' ? doLogin() : doRegister()"
        >
          {{ authMode === 'login' ? '登 录' : '注 册' }}
        </el-button>
      </el-form>
      <div class="auth-switch">
        <span v-if="authMode === 'login'">
          还没有账号？<el-link type="primary" @click="switchAuthMode('register')">去注册</el-link>
        </span>
        <span v-else>
          已有账号？<el-link type="primary" @click="switchAuthMode('login')">去登录</el-link>
        </span>
      </div>
    </div>
  </div>

  <!-- 已登录：主界面 -->
  <div v-else class="app">
    <!-- 左侧导航栏 -->
    <aside class="sidebar">
      <div class="sidebar-logo">
        <span class="sidebar-logo-icon">⇄</span>
        <span class="sidebar-logo-text">二手交易平台分析</span>
      </div>
      <nav class="sidebar-menu">
        <div
          v-for="m in visibleMenu"
          :key="m.name"
          class="sidebar-item"
          :class="{ active: activeTab === m.name }"
          @click="activeTab = m.name"
        >
          <span class="sidebar-icon">{{ m.icon }}</span>
          <span class="sidebar-label">{{ m.label }}</span>
          <span v-if="m.name === 'pending' && pending.length" class="sidebar-badge">
            {{ pending.length }}
          </span>
          <span v-else-if="m.adminOnly" class="sidebar-admin">admin</span>
        </div>
      </nav>
      <div class="sidebar-footer">
        <el-avatar :size="32" class="sidebar-avatar">
          {{ (currentUser?.username || '?').slice(0, 1).toUpperCase() }}
        </el-avatar>
        <div class="sidebar-user">
          <div class="sidebar-user-name">{{ currentUser?.username }}</div>
          <div class="sidebar-user-role">{{ isAdmin ? '管理员' : '普通用户' }}</div>
        </div>
        <el-dropdown trigger="click">
          <span class="sidebar-more">···</span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="openChangePwd">修改密码</el-dropdown-item>
              <el-dropdown-item divided @click="doLogout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </aside>

    <!-- 主区域 -->
    <div class="main-wrap">
      <header class="topbar">
        <div class="topbar-title">
          <span class="topbar-crumb">{{ currentTitle }}</span>
          <span class="topbar-sub">{{ currentSub }}</span>
        </div>
        <div class="topbar-right">
          <span class="rag-status" :class="{ ready: ragReady !== false }">
            <i class="dot" /> {{ ragReady === false ? '知识库未就绪' : '知识库就绪' }}
          </span>
          <el-tag v-if="isAdmin" type="warning" size="small" effect="dark">管理员</el-tag>
        </div>
      </header>

      <main class="main">
      <!-- 列表页 -->
      <div v-if="activeTab === 'list'">
        <!-- 统计卡片 -->
        <div class="stat-row">
          <div class="stat-card">
            <div class="stat-icon blue">📦</div>
            <div><div class="stat-val">{{ compStats.total }}</div><div class="stat-label">竞品总数</div></div>
          </div>
          <div class="stat-card">
            <div class="stat-icon green">✅</div>
            <div><div class="stat-val">{{ compStats.online }}</div><div class="stat-label">已上线</div></div>
          </div>
          <div class="stat-card">
            <div class="stat-icon orange">⏳</div>
            <div><div class="stat-val">{{ compStats.pending }}</div><div class="stat-label">待校对</div></div>
          </div>
          <div class="stat-card">
            <div class="stat-icon purple">📄</div>
            <div><div class="stat-val">{{ compStats.offline }}</div><div class="stat-label">未上线</div></div>
          </div>
        </div>

        <div class="toolbar">
          <el-input
            v-model="keyword"
            placeholder="搜索名称"
            clearable
            style="width: 220px"
            @keyup.enter="onSearch"
            @clear="onSearch"
          />
          <el-select
            v-model="filterType"
            placeholder="平台类型"
            clearable
            style="width: 140px"
            @change="onSearch"
          >
            <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
          </el-select>
          <el-button type="primary" @click="onSearch">查询</el-button>
          <span class="toolbar-spacer" />
          <el-button type="primary" plain @click="openCreate">+ 录入竞品</el-button>
          <el-button type="success" plain @click="openFetch">抓取竞品</el-button>
          <el-button
            type="warning"
            plain
            :disabled="selectedIds.length < 2"
            @click="goCompare"
          >
            对比 ({{ selectedIds.length }})
          </el-button>
          <el-button type="info" plain :loading="exporting" @click="doExportReport">
            导出报告{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
          </el-button>
        </div>

        <el-table
          v-loading="loading"
          :data="pagedCompetitors"
          border
          stripe
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="50" />
          <el-table-column prop="name" label="名称" min-width="140" />
          <el-table-column prop="type" label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="typeTag(row.type)" size="small">{{ row.type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="positioning" label="定位" min-width="200" show-overflow-tooltip />
          <el-table-column prop="launch_status" label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="statusTag(row.launch_status)" size="small">
                {{ row.launch_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="website" label="官网" min-width="160" show-overflow-tooltip />
          <el-table-column label="操作" width="140" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="list-pagination">
          <el-pagination
            v-model:current-page="compPage"
            :page-size="compPageSize"
            :total="competitors.length"
            layout="total, prev, pager, next"
            background
          />
        </div>
      </div>

      <!-- 待校对页 -->
      <div v-else-if="activeTab === 'pending'">
        <el-alert
          title="以下是从网页抓取并由 AI 提炼的竞品，请人工校对后确认入库"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 16px"
        />
        <el-table v-loading="pendingLoading" :data="pending" border stripe>
          <el-table-column prop="name" label="名称" min-width="140" />
          <el-table-column prop="type" label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="typeTag(row.type)" size="small">{{ row.type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="positioning" label="定位" min-width="200" show-overflow-tooltip />
          <el-table-column prop="website" label="来源" min-width="180" show-overflow-tooltip />
          <el-table-column prop="data_status" label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="dataStatusTag(row.data_status)" size="small">
                {{ row.data_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button link type="success" @click="approve(row)">确认入库</el-button>
              <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="count">共 {{ pending.length }} 条待校对</p>
      </div>

      <!-- 录入页 -->
      <div v-else-if="activeTab === 'form'" class="form-wrap">
        <el-form :model="form" label-width="100px">
          <h3 class="section">基础信息</h3>
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="名称" required>
                <el-input v-model="form.name" placeholder="必填" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="平台类型" required>
                <el-select v-model="form.type" style="width: 100%">
                  <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="定位">
            <el-input v-model="form.positioning" placeholder="一句话定位" />
          </el-form-item>
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="官网">
                <el-input v-model="form.website" placeholder="https://..." />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="上线状态">
                <el-select v-model="form.launch_status" style="width: 100%">
                  <el-option v-for="s in statusOptions" :key="s" :label="s" :value="s" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>

          <h3 class="section">功能与业务</h3>
          <el-form-item label="核心功能">
            <el-input v-model="form.features" type="textarea" :rows="2" />
          </el-form-item>
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="目标用户">
                <el-input v-model="form.target_users" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="业务模式">
                <el-input v-model="form.business_model" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="定价">
            <el-input v-model="form.pricing" />
          </el-form-item>

          <h3 class="section">评分（用于对比雷达图）</h3>
          <el-form-item
            v-for="d in ratingDims"
            :key="d"
            :label="d"
          >
            <el-slider
              v-model="form.ratings[d]"
              :min="0"
              :max="5"
              :step="1"
              show-stops
              style="width: 400px"
            />
          </el-form-item>

          <h3 class="section">评价与备注</h3>
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="优势">
                <el-input v-model="form.strengths" type="textarea" :rows="2" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="不足">
                <el-input v-model="form.weaknesses" type="textarea" :rows="2" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="标签">
            <el-select
              v-model="form.tags"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入后回车创建标签"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="form.remark" type="textarea" :rows="2" />
          </el-form-item>

          <el-form-item>
            <el-button type="primary" :loading="saving" @click="save">
              {{ isEdit ? '保存修改' : '保存竞品' }}
            </el-button>
            <el-button @click="cancel">取消</el-button>
          </el-form-item>
        </el-form>
      </div>

      <!-- 对比页 -->
      <div v-else-if="activeTab === 'compare'">
        <div class="toolbar">
          <el-button plain @click="activeTab = 'list'">← 返回选择</el-button>
          <el-button @click="loadCompare">刷新对比</el-button>
        </div>

        <div v-if="compareData && compareData.items.length" v-loading="compareLoading">
          <div class="card">
            <h3 class="card-title">评分雷达图对比</h3>
            <div ref="radarEl" class="radar" />
          </div>

          <div class="card">
            <h3 class="card-title">详细对比表</h3>
            <el-table :data="compareData.items" border stripe>
              <el-table-column prop="name" label="竞品" min-width="120" fixed="left" />
              <el-table-column prop="type" label="类型" width="80">
                <template #default="{ row }">
                  <el-tag :type="typeTag(row.type)" size="small">{{ row.type }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="launch_status" label="状态" width="90">
                <template #default="{ row }">
                  <el-tag :type="statusTag(row.launch_status)" size="small">
                    {{ row.launch_status }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="positioning" label="定位" min-width="200" show-overflow-tooltip />
              <el-table-column prop="features" label="核心功能" min-width="200" show-overflow-tooltip />
              <el-table-column prop="target_users" label="目标用户" min-width="140" show-overflow-tooltip />
              <el-table-column prop="business_model" label="业务模式" min-width="160" show-overflow-tooltip />
              <el-table-column prop="pricing" label="定价" min-width="120" show-overflow-tooltip />
              <el-table-column prop="strengths" label="优势" min-width="180" show-overflow-tooltip />
              <el-table-column prop="weaknesses" label="不足" min-width="180" show-overflow-tooltip />
            </el-table>
          </div>
        </div>

        <el-empty v-else-if="!compareLoading" description="请在列表页勾选至少 2 个竞品进行对比" />
      </div>

      <!-- 知识库问答页 -->
      <div v-else-if="activeTab === 'rag'" class="rag-wrap">
        <el-alert
          v-if="ragReady === false"
          title="知识库尚未就绪，请先在后端运行建库脚本（scripts/build_vector_store.py）生成向量库。"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        />
        <div class="chat-layout">
          <!-- 会话侧栏 -->
          <div class="rag-side">
            <div class="rag-side-actions">
              <el-button class="new-chat-btn" type="primary" plain @click="newRagSession">
                ＋ 新建对话
              </el-button>
              <el-tooltip :content="batchMode ? '退出批量删除' : '批量删除'" placement="top">
                <el-button
                  class="batch-btn"
                  :type="batchMode ? 'warning' : 'default'"
                  text
                  @click="toggleBatchMode"
                >
                  🗑
                </el-button>
              </el-tooltip>
            </div>

            <!-- 批量模式工具条 -->
            <div v-if="batchMode" class="rag-side-batchbar">
              <el-checkbox :model-value="allChecked" @change="toggleCheckAll">全选</el-checkbox>
              <el-button size="small" type="danger" text @click="doBatchDelete">
                删除({{ checkedSessions.length }})
              </el-button>
            </div>

            <div class="rag-side-list">
              <div
                v-for="s in ragSessions"
                :key="s.session_id"
                class="rag-side-item"
                :class="{ active: !batchMode && s.session_id === ragSessionId }"
                @click="batchMode ? toggleSessionCheck(s.session_id) : switchSession(s.session_id)"
              >
                <el-checkbox
                  v-if="batchMode"
                  :model-value="checkedSessions.includes(s.session_id)"
                  @change="toggleSessionCheck(s.session_id)"
                  @click.stop
                />
                <div class="rag-side-item-body">
                  <div class="rag-side-title" :title="s.first_question || '新对话'">
                    {{ s.first_question || '新对话' }}
                  </div>
                  <div class="rag-side-meta">
                    {{ s.message_count }} 条 · {{ fmtSessionTime(s.updated_at) }}
                  </div>
                </div>
              </div>
              <div v-if="!ragSessions.length" class="rag-side-empty">暂无会话</div>
            </div>
          </div>

          <!-- 对话区：气泡流 -->
          <div class="chat-main">
            <div class="chat-msgs" ref="chatMsgsEl">
              <!-- 空会话：欢迎语 -->
              <div v-if="!ragHistory.length && !ragLoading" class="chat-welcome">
                <div class="chat-welcome-icon">💬</div>
                <h3>二手交易知识库问答</h3>
                <p>
                  你好，我是二手交易平台的知识库助手。基于约 1000 篇平台语料（规则制度 / 买卖指南 / 验货质检 /
                  售后仲裁 / 案例 / 公告），命中知识库时直接返回原文并标注来源，未命中时由大模型兜底回答。
                  支持多轮追问。
                </p>
              </div>

              <!-- 消息流 -->
              <div
                v-for="(m, i) in ragHistory"
                :key="i"
                class="chat-msg"
                :class="m.role"
              >
                <div class="chat-avatar">{{ m.role === 'user' ? '我' : 'AI' }}</div>
                <div class="chat-body">
                  <div class="chat-bubble">{{ m.content }}</div>
                  <!-- AI 消息带来源引用 -->
                  <div
                    v-if="m.role === 'assistant' && m.sources && m.sources.length"
                    class="chat-sources"
                  >
                    <el-collapse>
                      <el-collapse-item
                        v-for="(s, j) in m.sources"
                        :key="j"
                        :name="j"
                      >
                        <template #title>
                          <div class="rag-src-title">
                            <el-tag size="small" type="info">{{ s.doc_id }}</el-tag>
                            <b>{{ s.title }}</b>
                            <span class="rag-score">相关度 {{ (s.score * 100).toFixed(1) }}%</span>
                          </div>
                        </template>
                        <div class="rag-src-body">
                          <p>{{ s.text }}</p>
                          <el-button size="small" text type="primary" @click.stop="copySourceText(s)">
                            复制来源文本
                          </el-button>
                        </div>
                      </el-collapse-item>
                    </el-collapse>
                  </div>
                </div>
              </div>

              <!-- 打字中 -->
              <div v-if="ragLoading" class="chat-msg assistant">
                <div class="chat-avatar">AI</div>
                <div class="chat-body">
                  <div class="chat-bubble chat-typing">
                    <span class="typing-dot" /><span class="typing-dot" /><span class="typing-dot" />
                  </div>
                </div>
              </div>
            </div>

            <!-- 底部输入框 -->
            <div class="chat-input-bar">
              <el-input
                v-model="ragQuestion"
                placeholder="输入问题，例如：忘记密码怎么办 / 卖家入驻需要什么资质"
                clearable
                @keyup.enter="doAsk"
              />
              <el-button type="primary" :loading="ragLoading" @click="doAsk">发送</el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 变化记录页 -->
      <div v-else-if="activeTab === 'changes'" class="changes-wrap">
        <div class="card">
          <div class="changes-head">
            <h3 class="card-title">竞品变化记录</h3>
            <el-button size="small" type="primary" plain @click="doMonitor">
              手动触发监控检查
            </el-button>
          </div>
          <p class="rag-tip">
            记录竞品字段的变化（人工编辑时自动追踪）。定时监控每小时自动做一次快照检查。
          </p>
          <el-table :data="changes" v-loading="changesLoading" border stripe>
            <el-table-column prop="competitor_name" label="竞品" min-width="120" />
            <el-table-column prop="field" label="字段" width="110" />
            <el-table-column label="旧值" min-width="180">
              <template #default="{ row }">
                <span class="muted">{{ row.old_value ?? '（空）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="新值" min-width="180">
              <template #default="{ row }">
                <span>{{ row.new_value ?? '（空）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.source === 'monitor' ? 'warning' : 'info'">
                  {{ row.source === 'monitor' ? '监控' : '人工' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="170">
              <template #default="{ row }">
                {{ (row.changed_at || '').replace('T', ' ').slice(0, 19) }}
              </template>
            </el-table-column>
          </el-table>
          <p v-if="!changes.length && !changesLoading" class="muted" style="margin-top: 16px">
            暂无变化记录。
          </p>
        </div>
      </div>

      <!-- 知识库管理页（admin 专属） -->
      <div v-else-if="activeTab === 'kb'" class="kb-wrap">
        <div class="card">
          <div class="changes-head">
            <h3 class="card-title">知识库文档管理</h3>
            <el-button type="primary" plain :loading="kbRebuilding" @click="doKbRebuild">
              重建向量库
            </el-button>
          </div>
          <p class="rag-tip">
            管理知识库语料（canonical 下的 Markdown + FAQ JSON）。上传/删除文档后需「重建向量库」才会进入检索。
            上传文件名需符合 <code>&lt;id&gt;_&lt;标题&gt;.md</code>（id 形如 A-001，且已在 seeds 中登记）。
          </p>

          <div class="kb-upload">
            <el-upload
              :auto-upload="false"
              :limit="1"
              accept=".md"
              :on-change="onKbFileChange"
              :on-remove="() => (kbUploadFile = null)"
            >
              <el-button>选择 .md 文件</el-button>
            </el-upload>
            <el-button type="primary" :disabled="!kbUploadFile" @click="doKbUpload">
              上传
            </el-button>
          </div>

          <el-table v-loading="kbLoading" :data="kbDocuments" border stripe>
            <el-table-column prop="filename" label="文件名" min-width="260" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="140" />
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.kind === 'md' ? 'primary' : 'success'">
                  {{ row.kind === 'md' ? 'MD' : 'FAQ' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ kbSize(row.size) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" @click="doKbDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="kb-pagination">
            <el-pagination
              v-model:current-page="kbPage"
              :page-size="kbPageSize"
              :total="kbTotal"
              layout="total, prev, pager, next, jumper"
              background
              @current-change="onKbPageChange"
            />
          </div>
        </div>
      </div>

      <!-- 设置页 -->
      <div v-else-if="activeTab === 'settings'" class="form-wrap">
        <h3 class="section">评分维度配置</h3>
        <el-alert
          title="评分维度用于雷达图、竞品评分与报告导出。修改后新维度立即生效；已录入竞品在新维度下无评分的按 0 分处理。"
          type="info"
          :closable="false"
          show-icon
          style="margin-bottom: 16px"
        />
        <div class="dims-list">
          <el-tag
            v-for="d in dimsDraft"
            :key="d"
            closable
            size="large"
            style="margin: 0 8px 8px 0"
            @close="removeDim(d)"
          >
            {{ d }}
          </el-tag>
        </div>
        <div class="dims-add">
          <el-input
            v-model="newDim"
            placeholder="输入新维度名称，如：验机能力"
            style="width: 280px"
            maxlength="12"
            @keyup.enter="addDim"
          />
          <el-button @click="addDim">添加</el-button>
        </div>
        <p class="count">当前 {{ dimsDraft.length }} 个维度（3~8 个）</p>
        <el-button type="primary" :loading="dimsSaving" @click="saveDims">保存配置</el-button>
      </div>

    <!-- 抓取对话框 -->
    <el-dialog v-model="fetchDialogVisible" title="抓取竞品" width="540px">
      <p class="fetch-tip">输入竞品官网 URL，系统将抓取页面并用 AI 提炼结构化信息，生成待校对草稿。</p>
      <el-input
        v-model="fetchUrl"
        placeholder="https://www.weipaitang.com/"
        clearable
        @keyup.enter="doFetch"
      />
      <div class="cost-bar" v-if="costStatus">
        <span>
          本会话 LLM 已消费
          <b>¥{{ costStatus.total_cost_cny }}</b>
          / 限额 ¥{{ costStatus.cost_limit_cny }}
          <el-tag v-if="costStatus.blocked" type="danger" size="small">已熔断</el-tag>
        </span>
        <el-button link type="primary" size="small" @click="resetCost">重置累计</el-button>
      </div>
      <template #footer>
        <el-button @click="fetchDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="fetching" @click="doFetch">抓取并提炼</el-button>
      </template>
    </el-dialog>

    <!-- 成本熔断征询对话框 -->
    <el-dialog v-model="costDialog" title="LLM 调用费用确认" width="480px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="本次 LLM 调用预估费用已超过会话限额，需你确认后才会真正发出请求。"
        style="margin-bottom: 14px"
      />
      <div v-if="costInfo" class="cost-detail">
        <p>
          本次预估费用：
          <b class="num">¥{{ costInfo.estimate_cost_cny }}</b>
        </p>
        <p>
          当前已累计：<b class="num">¥{{ costInfo.total_cost_cny }}</b>
          <span v-if="costInfo.projected_total_cny" class="muted">
            （累计后约 ¥{{ costInfo.projected_total_cny }}）
          </span>
        </p>
        <p class="muted">会话限额：¥{{ costInfo.cost_limit_cny }}</p>
      </div>
      <template #footer>
        <el-button @click="cancelFetch">取消本次</el-button>
        <el-button type="warning" :loading="costConfirming" @click="confirmFetch">
          同意，继续本次调用
        </el-button>
      </template>
    </el-dialog>

    <!-- 修改密码对话框 -->
    <el-dialog v-model="pwdDialog" title="修改密码" width="420px">
      <el-form @submit.prevent>
        <el-form-item label="旧密码">
          <el-input v-model="pwdOld" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwdNew" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="pwdConfirm" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialog = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="doChangePwd">确认修改</el-button>
      </template>
    </el-dialog>
    </main>
    </div>
  </div>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
}
.app {
  display: flex;
  min-height: 100vh;
  height: 100vh;
  overflow: hidden;
}
/* ===== 侧边栏 ===== */
.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: #1d2b36;
  color: #bfcbd9;
  display: flex;
  flex-direction: column;
}
.sidebar-logo {
  height: 60px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  white-space: nowrap;
}
.sidebar-logo-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #409eff, #7b5cff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}
.sidebar-logo-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}
.sidebar-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  height: 44px;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 14px;
  border-left: 3px solid transparent;
  white-space: nowrap;
}
.sidebar-item:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #fff;
}
.sidebar-item.active {
  background: rgba(64, 158, 255, 0.15);
  color: #fff;
  border-left-color: #409eff;
}
.sidebar-icon {
  width: 18px;
  text-align: center;
  font-size: 15px;
}
.sidebar-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-badge {
  margin-left: auto;
  background: #f56c6c;
  color: #fff;
  font-size: 11px;
  border-radius: 10px;
  padding: 1px 7px;
}
.sidebar-admin {
  margin-left: auto;
  font-size: 10px;
  color: #e6a23c;
  border: 1px solid #e6a23c;
  border-radius: 3px;
  padding: 0 4px;
}
.sidebar-footer {
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.sidebar-avatar {
  background: linear-gradient(135deg, #409eff, #67c23a);
  flex-shrink: 0;
}
.sidebar-user {
  flex: 1;
  min-width: 0;
}
.sidebar-user-name {
  color: #fff;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-user-role {
  font-size: 11px;
  color: #8a9bb0;
}
.sidebar-more {
  color: #8a9bb0;
  cursor: pointer;
  font-size: 16px;
  padding: 0 6px;
  outline: none;
}
.sidebar-more:hover {
  color: #fff;
}
/* ===== 主区域 ===== */
.main-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.topbar {
  height: 60px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}
.topbar-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.topbar-crumb {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}
.topbar-sub {
  font-size: 12px;
  color: #909399;
}
.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rag-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #67c23a;
}
.rag-status .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.rag-status.ready {
  color: #67c23a;
}
.rag-status:not(.ready) {
  color: #e6a23c;
}
.sub {
  margin: 2px 0 8px;
  color: #909399;
  font-size: 12px;
}
.main {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.toolbar-spacer {
  flex: 1;
}
/* 统计卡片 */
.stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.stat-card {
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
  padding: 18px 20px;
  display: flex;
  align-items: center;
  gap: 14px;
}
.stat-icon {
  width: 46px;
  height: 46px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  flex-shrink: 0;
}
.stat-icon.blue { background: #ecf5ff; }
.stat-icon.green { background: #f0f9eb; }
.stat-icon.orange { background: #fdf6ec; }
.stat-icon.purple { background: #f4f0ff; }
.stat-val {
  font-size: 24px;
  font-weight: 700;
  color: #303133;
  line-height: 1.2;
}
.stat-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.list-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.count {
  color: #909399;
  font-size: 13px;
  margin-top: 12px;
}
.form-wrap {
  background: #fff;
  padding: 24px;
  border-radius: 6px;
}
.section {
  margin: 16px 0 12px;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
  color: #303133;
  font-size: 15px;
}
.fetch-tip {
  color: #909399;
  font-size: 13px;
  margin: 0 0 12px;
}
.card {
  background: #fff;
  border-radius: 6px;
  padding: 20px;
  margin-bottom: 16px;
}
.card-title {
  margin: 0 0 16px;
  font-size: 15px;
  color: #303133;
}
.rag-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.rag-head .card-title {
  margin: 0;
}
.changes-wrap {
  max-width: 1100px;
}
.changes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.changes-head .card-title {
  margin: 0;
}
.radar {
  width: 100%;
  height: 420px;
}
.rag-wrap {
  max-width: 1100px;
}
.rag-tip {
  color: #909399;
  font-size: 13px;
  margin: 12px 0 16px;
  line-height: 1.6;
}
.rag-src-title {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.rag-src-title b {
  font-size: 13px;
  color: #303133;
}
.rag-src-body p {
  margin: 0 0 8px;
  color: #606266;
  background: #fafafa;
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.rag-score {
  color: #909399;
  font-size: 12px;
  margin-left: auto;
}
.dims-list {
  margin-bottom: 12px;
}
.dims-add {
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
}
.cost-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  color: #606266;
  font-size: 13px;
}
.cost-bar b {
  color: #e6a23c;
}
.cost-detail p {
  margin: 6px 0;
}
.cost-detail .num {
  color: #e6a23c;
}
.cost-detail .muted {
  color: #909399;
  font-size: 13px;
}
/* 登录页 */
.auth-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.auth-card {
  width: 380px;
  background: #fff;
  border-radius: 10px;
  padding: 40px 36px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.auth-brand {
  text-align: center;
  margin-bottom: 24px;
}
.auth-brand h1 {
  margin: 0;
  font-size: 20px;
  color: #303133;
}
.auth-brand .sub {
  margin: 6px 0 0;
}
.auth-submit {
  width: 100%;
}
.auth-switch {
  margin-top: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
/* 用户信息栏（侧边栏底部已承载用户信息，此样式废弃） */
/* 会话侧栏 + 聊天气泡流布局 */
.chat-layout {
  display: flex;
  gap: 16px;
  align-items: stretch;
  height: calc(100vh - 60px - 40px - 12px);
  min-height: 480px;
}
.rag-side {
  width: 260px;
  flex-shrink: 0;
  background: #fff;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.rag-side-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.new-chat-btn {
  flex: 1;
}
.batch-btn {
  font-size: 16px;
  padding: 8px 10px;
}
.rag-side-batchbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  flex-shrink: 0;
}
.rag-side-list {
  flex: 1;
  overflow-y: auto;
}
.rag-side-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
}
.rag-side-item:hover {
  background: #f5f7fa;
}
.rag-side-item.active {
  background: #ecf5ff;
  border-color: #c6e2ff;
}
.rag-side-item-body {
  min-width: 0;
  flex: 1;
}
.rag-side-title {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rag-side-meta {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.rag-side-empty {
  text-align: center;
  color: #c0c4cc;
  font-size: 13px;
  padding: 32px 0;
}
/* 对话区 */
.chat-main {
  flex: 1;
  min-width: 0;
  background: #fff;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.chat-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}
.chat-welcome {
  text-align: center;
  padding: 60px 20px 30px;
  max-width: 520px;
  margin: 0 auto;
}
.chat-welcome-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto 18px;
  border-radius: 18px;
  background: linear-gradient(135deg, #409eff, #7b5cff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 30px;
  color: #fff;
}
.chat-welcome h3 {
  font-size: 18px;
  margin-bottom: 10px;
  color: #303133;
}
.chat-welcome p {
  font-size: 13px;
  color: #909399;
  line-height: 1.8;
}
.chat-msg {
  display: flex;
  gap: 12px;
  margin-bottom: 22px;
}
.chat-msg.user {
  flex-direction: row-reverse;
}
.chat-avatar {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 13px;
}
.chat-msg.assistant .chat-avatar {
  background: linear-gradient(135deg, #409eff, #7b5cff);
}
.chat-msg.user .chat-avatar {
  background: linear-gradient(135deg, #67c23a, #409eff);
}
.chat-body {
  max-width: 72%;
  min-width: 0;
}
.chat-bubble {
  padding: 12px 16px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
  white-space: pre-wrap;
}
.chat-msg.assistant .chat-bubble {
  background: #f5f7fa;
  border-top-left-radius: 2px;
}
.chat-msg.user .chat-bubble {
  background: #409eff;
  color: #fff;
  border-top-right-radius: 2px;
}
.chat-sources {
  margin-top: 8px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fafbfc;
  overflow: hidden;
}
.chat-sources .el-collapse {
  border: none;
}
.chat-typing {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  padding: 14px 16px;
}
.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #c0c4cc;
  animation: typing-blink 1.2s infinite;
}
.typing-dot:nth-child(2) {
  animation-delay: 0.2s;
}
.typing-dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes typing-blink {
  0%, 80%, 100% { opacity: 0.3; }
  40% { opacity: 1; }
}
.chat-input-bar {
  border-top: 1px solid #e4e7ed;
  padding: 14px 20px;
  display: flex;
  gap: 10px;
  background: #fff;
  flex-shrink: 0;
}
.chat-input-bar .el-input {
  flex: 1;
}
.chat-input-bar .el-button {
  padding: 0 24px;
  border-radius: 6px;
}
/* 知识库管理 */
.kb-wrap {
  max-width: 1100px;
}
.kb-upload {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
}
.kb-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
