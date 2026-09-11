/**
 * 竞品侧共享状态 store（组合式函数 + 模块级单例，不引入 Pinia）。
 *
 * 为什么不上 Pinia：前端无测试基建、无 TS，Pinia 的 devtools / TS 类型优势都用不上；
 * 而「模块级 ref 单例」已能满足跨组件共享。将来若需要 devtools 时序调试、跨页面
 * 持久化或 SSR，state/action 形状可一一映射过去，迁移成本可控。
 *
 * 职责边界：壳层（App.vue）管「在哪一页」（activeTab），本 store 管「数据是什么」。
 *
 * 数据流约定（防「改 A 坏 B」）：
 * 1. 任何改了竞品数据的动作，只调 notifyChanged()，不关心谁需要刷新；
 * 2. 列表页的刷新由 listVersion 驱动（它自带搜索/筛选，必须各自拉取）；
 * 3. 待校对数据在本 store 内共享（列表页徽标与待校对页看到的是同一份），
 *    pendingCount 由 pendingRows 派生 —— 组件无法直接赋值，天然只有单一写入方。
 *
 * ⚠️ 用法：store 返回的是 ref 本身，组件必须**顶层解构**才能在模板里自动解包：
 *     const { pendingCount, listVersion } = useCompetitorStore()   // ✅
 *     const store = useCompetitorStore()  // 模板里 store.pendingCount 会渲染出 ref 对象 ❌
 */
import { ref, computed } from 'vue'
import { competitorApi } from '../api'

// —— 状态（模块级单例：所有 import 处共享同一份）——

// 待校对草稿：徽标、列表页统计卡、待校对页共用同一份数据，避免各拉一次
const pendingRows = ref([])
const pendingLoading = ref(false)
// 待校对数量：派生值，不在任何地方直接赋值（单一写入方 = loadPending）
const pendingCount = computed(() => pendingRows.value.length)

// 评分维度：录入表单与设置页共用
const ratingDims = ref([])

// 录入/编辑：formTarget 为待编辑竞品的**快照**（null = 新建），formKey 变化强制重建表单
const formTarget = ref(null)
const formKey = ref(0)

// 对比选中的竞品 id（由列表页勾选后写入）
const compareIds = ref([])

// —— 数据变更通知：用版本号而非回调（组件卸载后不留悬挂引用）——
// 竞品列表的刷新信号；待校对侧靠共享的 pendingRows 自动更新，无需版本号
const listVersion = ref(0)

/**
 * 拉取待校对草稿（徽标数量的唯一写入方）。
 * 失败静默：徽标不是关键路径，弹错反而打扰。
 */
async function loadPending() {
  pendingLoading.value = true
  try {
    pendingRows.value = await competitorApi.list({ data_status: '待校对' })
  } catch {
    /* 非关键，静默 */
  } finally {
    pendingLoading.value = false
  }
}

/** 加载评分维度（失败抛给调用方决定是否提示）。 */
async function loadRatingDims() {
  const r = await competitorApi.getRatingDimensions()
  ratingDims.value = r?.dimensions ?? []
}

/** 设置页保存成功后回写维度（新维度对雷达图/报告立即生效）。 */
function setRatingDims(dims) {
  ratingDims.value = Array.isArray(dims) ? dims : []
}

/** 打开「新建竞品」表单。 */
function openCreate() {
  formTarget.value = null
  formKey.value++
}

/**
 * 打开「编辑竞品」表单。
 * 存**深拷贝快照**而非列表数组里的行对象引用：列表被版本号驱动整体替换后，
 * 旧引用会变成游离对象，表单可能读到过期数据。
 */
function openEdit(row) {
  formTarget.value = row ? JSON.parse(JSON.stringify(row)) : null
  formKey.value++
}

/** 记录要对比的竞品 id 列表。 */
function startCompare(ids) {
  compareIds.value = Array.isArray(ids) ? [...ids] : []
}

/**
 * 任何「改了竞品数据」的动作只调这一个：统一触发列表与待校对（含徽标）同步。
 * 列表未挂载时 listVersion 自增无副作用，其 onMounted 会自行拉取。
 */
function notifyChanged() {
  listVersion.value++
  loadPending()
}

/** 退出登录时清空，避免下一个账号看到上一个账号的残留状态。 */
function reset() {
  pendingRows.value = []
  ratingDims.value = []
  formTarget.value = null
  formKey.value = 0
  compareIds.value = []
  listVersion.value = 0
}

export function useCompetitorStore() {
  return {
    // state
    pendingRows,
    pendingCount,
    pendingLoading,
    ratingDims,
    formTarget,
    formKey,
    compareIds,
    listVersion,
    // actions
    loadPending,
    loadRatingDims,
    setRatingDims,
    openCreate,
    openEdit,
    startCompare,
    notifyChanged,
    reset,
  }
}
