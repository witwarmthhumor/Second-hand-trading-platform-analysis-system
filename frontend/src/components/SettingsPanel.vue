<script setup>
/**
 * 设置页：评分维度配置。
 *
 * 维度变更会影响雷达图、竞品评分与报告导出，保存成功后 emit saved，
 * 由 App 更新全局 ratingDims 并传递给录入表单。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { competitorApi } from '../api'

const props = defineProps({
  dims: { type: Array, default: () => [] },
})
const emit = defineEmits(['saved'])

const MIN_DIMENSIONS = 3
const MAX_DIMENSIONS = 8

const dimsDraft = ref([])
const newDim = ref('')
const dimsSaving = ref(false)

watch(
  () => props.dims,
  (d) => {
    dimsDraft.value = [...(d || [])]
  },
  { immediate: true },
)

function addDim() {
  const d = newDim.value.trim()
  if (!d) return
  if (dimsDraft.value.includes(d)) {
    ElMessage.warning('该维度已存在')
    return
  }
  if (dimsDraft.value.length >= MAX_DIMENSIONS) {
    ElMessage.warning(`最多 ${MAX_DIMENSIONS} 个维度（雷达图过多难以阅读）`)
    return
  }
  dimsDraft.value.push(d)
  newDim.value = ''
}

function removeDim(d) {
  if (dimsDraft.value.length <= MIN_DIMENSIONS) {
    ElMessage.warning(`至少保留 ${MIN_DIMENSIONS} 个维度`)
    return
  }
  dimsDraft.value = dimsDraft.value.filter((x) => x !== d)
}

async function saveDims() {
  if (dimsDraft.value.length < MIN_DIMENSIONS) {
    ElMessage.warning(`至少 ${MIN_DIMENSIONS} 个维度`)
    return
  }
  dimsSaving.value = true
  try {
    const r = await competitorApi.setRatingDimensions(dimsDraft.value)
    emit('saved', r.dimensions)
    ElMessage.success('评分维度已更新（新维度对雷达图、对比与报告立即生效）')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    dimsSaving.value = false
  }
}
</script>

<template>
  <div class="form-wrap">
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
    <p class="count">当前 {{ dimsDraft.length }} 个维度（{{ MIN_DIMENSIONS }}~{{ MAX_DIMENSIONS }} 个）</p>
    <el-button type="primary" :loading="dimsSaving" @click="saveDims">保存配置</el-button>
  </div>
</template>

<style>
.dims-list {
  margin-bottom: 12px;
}
.dims-add {
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
}
</style>
