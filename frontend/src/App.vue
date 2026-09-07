<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { competitorApi } from './api'

const activeTab = ref('list')

const competitors = ref([])
const loading = ref(false)
const keyword = ref('')
const filterType = ref('')

const typeOptions = ['鉴定', '交易', '行情', '社区']
const statusOptions = ['已上线', '内测', '已下线']

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
    if (isEdit.value) {
      await competitorApi.update(editId.value, form.value)
      ElMessage.success('已更新')
    } else {
      await competitorApi.create(form.value)
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
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function typeTag(type) {
  const map = { 鉴定: 'warning', 交易: 'primary', 行情: 'success', 社区: 'info' }
  return map[type] || 'info'
}

function statusTag(s) {
  const map = { 已上线: 'success', 内测: 'warning', 已下线: 'info' }
  return map[s] || 'info'
}

onMounted(fetchList)
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
        <el-tab-pane label="录入竞品" name="form" />
        <el-tab-pane label="对比" name="compare" />
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
        </div>

        <el-table v-loading="loading" :data="competitors" border stripe>
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

      <!-- 对比页（占位，阶段 3 实现） -->
      <div v-else>
        <el-empty description="多竞品对比功能将在阶段 3 实现" />
      </div>
    </el-main>
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
</style>
