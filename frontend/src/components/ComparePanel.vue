<script setup>
/**
 * 多竞品对比页：评分雷达图 + 详细对比表。
 *
 * 通过 ids 属性接收要对比的竞品 id 列表（由列表页勾选后传入）；
 * 组件自持对比数据与 echarts 实例，切换/刷新时重建图表。
 */
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts/core'
import { RadarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { competitorApi } from '../api'
import { typeTag, statusTag } from '../utils/tags'

echarts.use([RadarChart, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps({
  ids: { type: Array, default: () => [] },
})
const emit = defineEmits(['back'])

const compareData = ref(null)
const compareLoading = ref(false)
const radarEl = ref(null)
let radarChart = null

async function loadCompare() {
  if (props.ids.length < 2) return
  compareLoading.value = true
  try {
    compareData.value = await competitorApi.compare(props.ids)
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

// 窗口尺寸变化时重绘，避免图表被拉伸变形
function onResize() {
  if (radarChart) radarChart.resize()
}

watch(() => props.ids, loadCompare, { immediate: true })
onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (radarChart) {
    radarChart.dispose()
    radarChart = null
  }
})
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button plain @click="emit('back')">← 返回选择</el-button>
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
          <el-table-column
            prop="target_users"
            label="目标用户"
            min-width="140"
            show-overflow-tooltip
          />
          <el-table-column
            prop="business_model"
            label="业务模式"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column prop="pricing" label="定价" min-width="120" show-overflow-tooltip />
          <el-table-column prop="strengths" label="优势" min-width="180" show-overflow-tooltip />
          <el-table-column prop="weaknesses" label="不足" min-width="180" show-overflow-tooltip />
        </el-table>
      </div>
    </div>

    <el-empty
      v-else-if="!compareLoading"
      description="请在列表页勾选至少 2 个竞品进行对比"
    />
  </div>
</template>

<style>
.radar {
  width: 100%;
  height: 420px;
}
</style>
