<script setup>
/**
 * 待校对页：AI 抓取提炼出的草稿列表，人工确认后入库。
 *
 * 草稿数据收敛在 store 内共享（徽标数量与列表页统计卡看到的是同一份），
 * 因此本页不再自持数据、也不再向上抛 count —— 避免同一份数据被反复拉取。
 * 增删改后仍通过 changed 事件通知壳层（M3 step3 将统一为 store.notifyChanged）。
 */
import { onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { competitorApi } from '../api'
import { useCompetitorStore } from '../stores/competitor'
import { typeTag, dataStatusTag } from '../utils/tags'

const emit = defineEmits(['edit', 'changed'])

const { pendingRows, pendingLoading, loadPending } = useCompetitorStore()

async function approve(row) {
  try {
    await competitorApi.approve(row.id)
    ElMessage.success('已确认入库')
    emit('changed')
  } catch (e) {
    ElMessage.error(e.message)
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
    emit('changed')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(loadPending)
</script>

<template>
  <div>
    <el-alert
      title="以下是从网页抓取并由 AI 提炼的竞品，请人工校对后确认入库"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    />
    <el-table v-loading="pendingLoading" :data="pendingRows" border stripe>
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
          <el-button link type="primary" @click="emit('edit', row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 空态：说明「待校对」的来源，避免新用户困惑 -->
    <el-empty
      v-if="!pendingRows.length && !pendingLoading"
      description="暂无待校对草稿"
    >
      <p class="empty-guide-tip">
        在「竞品列表」页点击「抓取竞品」输入官网 URL，AI 提炼后的结果会出现在这里等待确认。
      </p>
    </el-empty>
    <p v-else class="count">共 {{ pendingRows.length }} 条待校对</p>
  </div>
</template>

<style>
.empty-guide-tip {
  color: #909399;
  font-size: 13px;
  margin-top: 12px;
}
</style>
