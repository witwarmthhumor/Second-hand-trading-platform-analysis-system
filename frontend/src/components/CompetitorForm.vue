<script setup>
/**
 * 竞品录入 / 编辑表单。
 *
 * 通过 model 属性接收待编辑的竞品行（null 表示新建）；保存成功后 emit saved，
 * 由 App 决定后续跳转与列表刷新，表单自身不关心导航。
 */
import { ref, watch, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { competitorApi } from '../api'
import { useCompetitorStore } from '../stores/competitor'
import { TYPE_OPTIONS, STATUS_OPTIONS } from '../utils/tags'

const props = defineProps({
  // 待编辑的竞品对象；null 表示新建
  model: { type: Object, default: null },
})
const emit = defineEmits(['saved', 'cancel'])

// 评分维度（来自设置页配置）由 store 统一维护
const { ratingDims } = useCompetitorStore()

const typeOptions = TYPE_OPTIONS
const statusOptions = STATUS_OPTIONS

const saving = ref(false)
const isEdit = computed(() => !!(props.model && props.model.id))

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

const form = ref(emptyForm())

// 由 model 构建表单：编辑时回填字段与已有评分，新建时用空表单
function buildForm(row) {
  if (!row || !row.id) {
    form.value = emptyForm()
    return
  }
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
}

// model 或评分维度变化时重建表单（维度变了，评分项也要跟着变）
watch(() => props.model, buildForm, { immediate: true })
watch(ratingDims, () => buildForm(props.model))

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
      await competitorApi.update(props.model.id, payload)
      ElMessage.success('已更新')
    } else {
      await competitorApi.create(payload)
      ElMessage.success('已录入')
    }
    emit('saved')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="form-wrap">
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
      <el-form-item v-for="d in ratingDims" :key="d" :label="d">
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
        <el-button @click="emit('cancel')">取消</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>
