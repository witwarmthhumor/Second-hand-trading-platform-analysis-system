<script setup>
/**
 * 竞品列表页：统计卡片 + 搜索筛选 + 列表 + 前端分页 + 抓取入口。
 *
 * 列表数据与分页/筛选状态自持（列表带搜索/筛选，无法与其他页共享）；
 * 刷新由 store.listVersion 驱动，**不再依赖「切 tab 重新挂载」这一副作用** ——
 * 因此本页的任何增删都只调 store.notifyChanged()，自己不重拉（否则一次操作拉两遍）。
 * 跨页动作的数据状态写入 store，页面切换通过 navigate 事件交给壳层。
 */
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { competitorApi } from '../api'
import { useCompetitorStore } from '../stores/competitor'
import { typeTag, statusTag, TYPE_OPTIONS } from '../utils/tags'

// 待校对数量来自 store（与侧栏徽标同源），此处仅用于统计卡片展示
const { pendingCount, listVersion, openCreate, openEdit, startCompare, notifyChanged } =
  useCompetitorStore()

// navigate 只表达「要去哪一页」，数据状态一律走 store
const emit = defineEmits(['navigate'])

const competitors = ref([])
const loading = ref(false)
const keyword = ref('')
const filterType = ref('')
const compPage = ref(1)
const compPageSize = 10

const selectedIds = ref([])
const exporting = ref(false)

const typeOptions = TYPE_OPTIONS

// 统计卡片：总数/已上线/待校对/未上线
const compStats = computed(() => {
  const total = competitors.value.length
  const online = competitors.value.filter((c) => c.launch_status === '已上线').length
  return { total, online, offline: total - online, pending: pendingCount.value }
})

// 前端分页切片
const pagedCompetitors = computed(() => {
  const start = (compPage.value - 1) * compPageSize
  return competitors.value.slice(start, start + compPageSize)
})

// 空态判定：无数据且未施加筛选（此时才提示「去录入/抓取」，而不是「没有匹配结果」）
const isFiltered = computed(() => !!keyword.value || !!filterType.value)
const showEmptyGuide = computed(
  () => !loading.value && competitors.value.length === 0 && !isFiltered.value,
)

// 稀疏引导：只有 1~3 条数据时给出「下一步做什么」的清单（0 条走上面的完整空态引导）。
// 关闭状态记在 localStorage，用户点过「不再提示」后就不再打扰。
const ONBOARD_KEY = 'competitor_onboard_dismissed'
const onboardDismissed = ref(localStorage.getItem(ONBOARD_KEY) === '1')
const showOnboard = computed(
  () =>
    !loading.value &&
    !isFiltered.value &&
    competitors.value.length > 0 &&
    competitors.value.length <= 3 &&
    !onboardDismissed.value,
)

function dismissOnboard() {
  onboardDismissed.value = true
  localStorage.setItem(ONBOARD_KEY, '1')
}

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

// —— 跨页动作：写 store + 请求壳层切页 ——
function goCreate() {
  openCreate()
  emit('navigate', 'form')
}

function goEdit(row) {
  openEdit(row)
  emit('navigate', 'form')
}

function onSelectionChange(rows) {
  selectedIds.value = rows.map((r) => r.id)
}

function goCompare() {
  if (selectedIds.value.length < 2) {
    ElMessage.warning('请至少勾选 2 个竞品进行对比')
    return
  }
  startCompare(selectedIds.value)
  emit('navigate', 'compare')
}

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

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.name}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await competitorApi.remove(row.id)
    ElMessage.success('已删除')
    // 只广播，不自己重拉：listVersion 变化会触发下面的 watch
    notifyChanged()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// —— 抓取竞品：URL 对话框 + 成本熔断征询 ——
const fetchUrl = ref('')
const fetching = ref(false)
const fetchDialogVisible = ref(false)
const costDialog = ref(false)
const costInfo = ref(null) // 待审批信息 { approval_token, estimate_cost_cny, ... }
const costStatus = ref(null) // 累计状态
const costConfirming = ref(false)
let pendingFetchUrl = '' // 用户同意后要重试的 URL

function openFetch() {
  fetchUrl.value = ''
  fetchDialogVisible.value = true
  loadCostStatus()
}

async function loadCostStatus() {
  try {
    costStatus.value = await competitorApi.costStatus()
  } catch {
    /* 非关键，静默 */
  }
}

function openCostApprove(info, url) {
  costInfo.value = info
  pendingFetchUrl = url
  costDialog.value = true
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
    // 新草稿入库待校对：广播后列表与徽标自动同步
    notifyChanged()
    loadCostStatus()
  } catch (e) {
    if (e.status === 402 && e.costDetail) {
      // 成本熔断：弹框征询是否同意继续本次调用
      openCostApprove(e.costDetail, url)
      loadCostStatus()
    } else {
      ElMessage.error(e.message)
    }
  }
}

async function confirmFetch() {
  costConfirming.value = true
  try {
    await competitorApi.costApprove(costInfo.value.approval_token)
    costDialog.value = false
    await doFetchUrl(pendingFetchUrl)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    costConfirming.value = false
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

onMounted(fetchList)

// 数据变更广播 → 重拉列表（替代原先「切 tab 重新挂载才刷新」的隐式行为）
watch(listVersion, fetchList)

defineExpose({ fetchList })
</script>

<template>
  <div>
    <!-- 统计卡片 -->
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-icon blue">📦</div>
        <div>
          <div class="stat-val">{{ compStats.total }}</div>
          <div class="stat-label">竞品总数</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon green">✅</div>
        <div>
          <div class="stat-val">{{ compStats.online }}</div>
          <div class="stat-label">已上线</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon orange">⏳</div>
        <div>
          <div class="stat-val">{{ compStats.pending }}</div>
          <div class="stat-label">待校对</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon purple">📄</div>
        <div>
          <div class="stat-val">{{ compStats.offline }}</div>
          <div class="stat-label">未上线</div>
        </div>
      </div>
    </div>

    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索名称"
        clearable
        style="width: 220px"
        @keyup.enter="fetchList"
        @clear="fetchList"
      />
      <el-select
        v-model="filterType"
        placeholder="平台类型"
        clearable
        style="width: 140px"
        @change="fetchList"
      >
        <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
      </el-select>
      <el-button type="primary" @click="fetchList">查询</el-button>
      <span class="toolbar-spacer" />
      <el-button type="primary" plain @click="goCreate">+ 录入竞品</el-button>
      <el-button type="success" plain @click="openFetch">抓取竞品</el-button>
      <el-button type="warning" plain :disabled="selectedIds.length < 2" @click="goCompare">
        对比 ({{ selectedIds.length }})
      </el-button>
      <el-button type="info" plain :loading="exporting" @click="doExportReport">
        导出报告{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
      </el-button>
    </div>

    <!-- 稀疏引导：数据量少时给出明确的下一步动作（可关闭） -->
    <div v-if="showOnboard" class="onboard">
      <div class="onboard-icon">💡</div>
      <div class="onboard-body">
        <div class="onboard-title">竞品数量还不多，补几条再做对比更有意义</div>
        <ol class="onboard-steps">
          <li>
            用「<b>+ 录入竞品</b>」手动填写，或「<b>抓取竞品</b>」输入官网 URL 由 AI 自动提炼（建议 ≥3 个）
          </li>
          <li>到「<b>待校对</b>」页确认 AI 提炼结果，草稿入库后才参与对比与报告</li>
          <li>回到本页勾选 <b>2 个以上</b> → 点「对比」看雷达图，或「导出报告」生成 Markdown</li>
          <li>想在资料里直接查答案，可去「<b>知识库问答</b>」用自然语言提问</li>
        </ol>
      </div>
      <el-button link type="primary" size="small" @click="dismissOnboard">不再提示</el-button>
    </div>

    <!-- 空态引导：新用户/无数据时给出明确下一步 -->
    <el-empty v-if="showEmptyGuide" description="还没有任何竞品数据">
      <el-button type="primary" @click="goCreate">手动录入竞品</el-button>
      <el-button type="success" plain @click="openFetch">从官网抓取</el-button>
      <p class="empty-guide-tip">
        提示：录入或抓取后，状态为「待校对」的数据需在「待校对」页确认入库才会参与对比与报告。
      </p>
    </el-empty>

    <template v-else>
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
            <el-button link type="primary" @click="goEdit(row)">编辑</el-button>
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
    </template>

    <!-- 抓取对话框 -->
    <el-dialog v-model="fetchDialogVisible" title="抓取竞品" width="540px">
      <p class="fetch-tip">
        输入竞品官网 URL，系统将抓取页面并用 AI 提炼结构化信息，生成待校对草稿。
      </p>
      <el-input
        v-model="fetchUrl"
        placeholder="https://www.weipaitang.com/"
        clearable
        @keyup.enter="doFetch"
      />
      <div v-if="costStatus" class="cost-bar">
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
  </div>
</template>

<style>
/* 统计卡片（仅本页使用） */
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
.stat-icon.blue {
  background: #ecf5ff;
}
.stat-icon.green {
  background: #f0f9eb;
}
.stat-icon.orange {
  background: #fdf6ec;
}
.stat-icon.purple {
  background: #f4f0ff;
}
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
.fetch-tip {
  color: #909399;
  font-size: 13px;
  margin: 0 0 12px;
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
.empty-guide-tip {
  color: #909399;
  font-size: 13px;
  margin-top: 12px;
}
/* 稀疏引导横幅（仅本页使用） */
.onboard {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  background: #ecf5ff;
  border: 1px solid #d9ecff;
  border-radius: 6px;
  padding: 14px 16px;
  margin-bottom: 16px;
}
.onboard-icon {
  font-size: 18px;
  line-height: 1.4;
  flex-shrink: 0;
}
.onboard-body {
  flex: 1;
  min-width: 0;
}
.onboard-title {
  font-size: 14px;
  color: #303133;
  font-weight: 600;
  margin-bottom: 6px;
}
.onboard-steps {
  margin: 0;
  padding-left: 20px;
  color: #606266;
  font-size: 13px;
  line-height: 1.9;
}
.onboard-steps b {
  color: #409eff;
}
</style>
