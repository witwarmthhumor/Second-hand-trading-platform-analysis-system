<script setup>
/**
 * 反馈管理面板（仅管理员）：问答反馈回流可视化。
 *
 * 顶部聚合统计（总量 / 好评 / 差评 / 好评率 + 按命中模式交叉分布），下方分页
 * 列出反馈明细，展开可看问题与回答原文，用于定位「哪些问题被点差评」，
 * 作为检索质量评测与调参的证据来源。
 */
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { competitorApi } from '../api'
import { fmtTime } from '../utils/format'

const loading = ref(false)
const stats = ref(null)
const list = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

// mode → 展示名（synth 为 P1-② 合成答案路径）
const MODE_LABELS = { hit: '检索命中直出', synth: 'LLM 合成', fallback: 'LLM 兜底生成' }
const modeLabel = (m) => MODE_LABELS[m] || m

async function loadStats() {
  try {
    stats.value = await competitorApi.ragFeedbackStats()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function loadList() {
  loading.value = true
  try {
    const r = await competitorApi.ragFeedbackList(page.value, pageSize.value)
    list.value = r.items
    total.value = r.total
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function onPageChange(p) {
  page.value = p
  loadList()
}

onMounted(() => {
  loadStats()
  loadList()
})
</script>

<template>
  <div class="feedback-wrap">
    <!-- 聚合统计卡片 -->
    <div class="fb-stat-row">
      <div class="fb-stat-card">
        <div class="fb-num">{{ stats ? stats.total : '-' }}</div>
        <div class="fb-label">反馈总量</div>
      </div>
      <div class="fb-stat-card">
        <div class="fb-num good">{{ stats ? stats.like : '-' }}</div>
        <div class="fb-label">好评</div>
      </div>
      <div class="fb-stat-card">
        <div class="fb-num bad">{{ stats ? stats.dislike : '-' }}</div>
        <div class="fb-label">差评</div>
      </div>
      <div class="fb-stat-card">
        <div class="fb-num">
          {{ stats && stats.total ? (stats.like_ratio * 100).toFixed(1) + '%' : '-' }}
        </div>
        <div class="fb-label">好评率</div>
      </div>
    </div>

    <!-- 按回答模式交叉分布 -->
    <el-card v-if="stats" class="fb-card" shadow="never">
      <template #header><b>按回答模式分布</b></template>
      <div class="fb-mode-grid">
        <div v-for="(m, key) in stats.by_mode" :key="key" class="fb-mode-item">
          <div class="fb-mode-name">{{ modeLabel(key) }}</div>
          <div class="fb-mode-bar">
            <span class="good">好评 {{ m.like }}</span>
            <span class="bad">差评 {{ m.dislike }}</span>
          </div>
        </div>
        <div v-if="!Object.keys(stats.by_mode).length" class="fb-mode-empty">暂无反馈数据</div>
      </div>
    </el-card>

    <!-- 反馈明细 -->
    <el-card class="fb-card" shadow="never">
      <template #header><b>反馈明细（共 {{ total }} 条）</b></template>
      <el-table v-loading="loading" :data="list" stripe>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="fb-expand">
              <div class="fb-expand-q"><b>问题：</b>{{ row.question }}</div>
              <div class="fb-expand-a"><b>回答：</b>{{ row.answer || '（空）' }}</div>
              <div v-if="row.source_doc_ids && row.source_doc_ids.length" class="fb-expand-src">
                <b>来源文档：</b>{{ row.source_doc_ids.join('、') }}
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="评分" width="90">
          <template #default="{ row }">
            <el-tag :type="row.rating === 'like' ? 'success' : 'danger'" size="small">
              {{ row.rating === 'like' ? '好评' : '差评' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="question" label="问题" min-width="240" show-overflow-tooltip />
        <el-table-column label="模式" width="120">
          <template #default="{ row }">{{ modeLabel(row.mode) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-pagination
        class="fb-pager"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="pageSize"
        :current-page="page"
        @current-change="onPageChange"
      />
    </el-card>
  </div>
</template>

<style>
.fb-stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.fb-stat-card {
  background: var(--surface);
  border: 1px solid var(--line-1);
  border-radius: 8px;
  padding: 20px;
  text-align: center;
}
.fb-num {
  font-size: 28px;
  font-weight: 700;
  color: var(--ink-1);
}
.fb-num.good {
  color: var(--el-color-success);
}
.fb-num.bad {
  color: var(--el-color-danger);
}
.fb-label {
  margin-top: 6px;
  font-size: 13px;
  color: var(--ink-3);
}
.fb-card {
  margin-bottom: 16px;
}
.fb-mode-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}
.fb-mode-item {
  min-width: 160px;
}
.fb-mode-name {
  font-size: 13px;
  color: var(--ink-2);
  margin-bottom: 6px;
}
.fb-mode-bar {
  display: flex;
  gap: 12px;
  font-size: 14px;
}
.fb-mode-bar .good {
  color: var(--el-color-success);
}
.fb-mode-bar .bad {
  color: var(--el-color-danger);
}
.fb-mode-empty {
  color: var(--ink-3);
}
.fb-pager {
  margin-top: 14px;
  justify-content: flex-end;
}
.fb-expand {
  padding: 4px 16px 12px;
  font-size: 13px;
  color: var(--ink-2);
  line-height: 1.7;
}
.fb-expand-q {
  margin-bottom: 6px;
}
.fb-expand-a {
  margin-bottom: 6px;
}
.fb-expand-src {
  color: var(--ink-3);
}
</style>
