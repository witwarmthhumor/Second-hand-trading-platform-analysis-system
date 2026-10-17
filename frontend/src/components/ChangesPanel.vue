<script setup>
/**
 * 变化记录页：展示竞品字段的变化历史，并提供手动触发监控。
 *
 * 变化来源有两类：人工编辑（编辑竞品时自动记录）与定时监控（重抓对比）。
 */
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { competitorApi } from '../api'
import { useCompetitorStore } from '../stores/competitor'

// 监控轮次可能改动竞品字段，完成后同样广播数据变更（列表/徽标随之同步）
const { notifyChanged } = useCompetitorStore()

const changes = ref([])
const changesLoading = ref(false)
const monitoring = ref(false)

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
  monitoring.value = true
  try {
    const r = await competitorApi.triggerMonitor()
    // 后端返回可读提示（含检查数/更新数/熔断说明），优先透出
    if (r.cost_blocked) {
      ElMessage.warning(r.message)
    } else {
      ElMessage.success(r.message || `监控完成：检查 ${r.checked} 个竞品`)
    }
    await loadChanges()
    notifyChanged()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    monitoring.value = false
  }
}

onMounted(loadChanges)
</script>

<template>
  <div class="changes-wrap">
    <div class="card">
      <div class="changes-head">
        <h3 class="card-title">竞品变化记录</h3>
        <el-button
          size="small"
          type="primary"
          plain
          :loading="monitoring"
          @click="doMonitor"
        >
          手动触发监控检查
        </el-button>
      </div>
      <p class="rag-tip">
        记录竞品字段的变化：人工编辑竞品时自动追踪；定时监控按
        <code>MONITOR_INTERVAL_HOURS</code> 周期重抓竞品官网并比对（需在 .env 中开启
        <code>MONITOR_ENABLED=true</code>）。手动触发仅管理员可用。
      </p>
      <el-table v-loading="changesLoading" :data="changes" border stripe>
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

      <el-empty v-if="!changes.length && !changesLoading" description="暂无变化记录">
        <p class="empty-guide-tip">
          两种产生方式：① 在「竞品列表」编辑任一竞品字段；② 点上方「手动触发监控检查」
          重抓竞品官网并比对差异。
        </p>
      </el-empty>
    </div>
  </div>
</template>

<style>
.changes-wrap {
  max-width: 1100px;
}
.empty-guide-tip {
  color: var(--ink-3);
  font-size: 13px;
  margin-top: 12px;
}
</style>
