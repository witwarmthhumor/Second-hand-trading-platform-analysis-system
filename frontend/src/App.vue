<script setup>
import { ref, onMounted, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// echarts 按需引入：只注册用到的雷达图组件，避免全量打包（chunk 从 ~2.2MB 降到 ~500KB）
import * as echarts from 'echarts/core'
import { RadarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { competitorApi } from './api'

echarts.use([RadarChart, TooltipComponent, LegendComponent, CanvasRenderer])

const activeTab = ref('list')

const competitors = ref([])
const loading = ref(false)
const keyword = ref('')
const filterType = ref('')

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

const typeOptions = ['鉴定', '交易', '行情', '社区']
const statusOptions = ['已上线', '内测', '已下线']
// 评分维度：从后端设置读取（可在"设置"页配置）
const DEFAULT_DIMS = ['功能完整度', '用户规模', '鉴定能力', '价格竞争力', '口碑']
const ratingDims = ref([...DEFAULT_DIMS])

// —— 列表 ——
async function fetchList() {
  loading.value = true
  try {
    const params = {}
    if (keyword.value) params.keyword = keyword.value
    if (filterType.value) params.type = filterType.value
    competitors.value = await competitorApi.list(params)
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
    type: '交易',
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
  const map = { 鉴定: 'warning', 交易: 'primary', 行情: 'success', 社区: 'info' }
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

onMounted(() => {
  fetchList()
  fetchPending()
  loadDims()
  loadCostStatus()
})
</script>

<template>
  <el-container class="app">
    <el-header class="header">
      <div>
        <h1>古玩 · 钱币竞品分析系统</h1>
        <p class="sub">竞品情报收集、整理与横向对比</p>
      </div>
      <el-tabs v-model="activeTab" class="tabs">
        <el-tab-pane label="竞品列表" name="list" />
        <el-tab-pane label="待校对" name="pending" />
        <el-tab-pane label="录入竞品" name="form" />
        <el-tab-pane label="对比" name="compare" />
        <el-tab-pane label="设置" name="settings" />
      </el-tabs>
    </el-header>

    <el-main class="main">
      <!-- 列表页 -->
      <div v-if="activeTab === 'list'">
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
          :data="competitors"
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
        <p class="count">共 {{ competitors.length }} 条竞品</p>
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
            placeholder="输入新维度名称，如：社区活跃度"
            style="width: 280px"
            maxlength="12"
            @keyup.enter="addDim"
          />
          <el-button @click="addDim">添加</el-button>
        </div>
        <p class="count">当前 {{ dimsDraft.length }} 个维度（3~8 个）</p>
        <el-button type="primary" :loading="dimsSaving" @click="saveDims">保存配置</el-button>
      </div>
    </el-main>

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
  </el-container>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
}
.app {
  min-height: 100vh;
}
.header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding: 12px 24px 0;
  height: auto;
}
.header h1 {
  margin: 0;
  font-size: 18px;
}
.sub {
  margin: 2px 0 8px;
  color: #909399;
  font-size: 12px;
}
.tabs {
  margin-left: 40px;
}
.main {
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
  padding: 20px 24px;
}
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
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
.radar {
  width: 100%;
  height: 420px;
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
</style>
