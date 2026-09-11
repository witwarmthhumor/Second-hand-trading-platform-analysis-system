<script setup>
/**
 * 知识库管理页（管理员专属）：语料上传 / 删除 / 重建向量库。
 *
 * 上传或删除文档后必须重建向量库才会进入检索，页面已明确提示这一点。
 */
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { kbApi } from '../api'

const kbDocuments = ref([])
const kbLoading = ref(false)
const kbRebuilding = ref(false)
const kbUploadFile = ref(null)
const kbPage = ref(1)
const kbPageSize = 20
const kbTotal = ref(0)

async function loadKbDocuments() {
  kbLoading.value = true
  try {
    const r = await kbApi.listDocuments(kbPage.value, kbPageSize)
    kbDocuments.value = r.items
    kbTotal.value = r.total
  } catch (e) {
    ElMessage.error('加载知识库文档失败：' + e.message)
  } finally {
    kbLoading.value = false
  }
}

function onKbPageChange(p) {
  kbPage.value = p
  loadKbDocuments()
}

function onKbFileChange(file) {
  kbUploadFile.value = file.raw
}

async function doKbUpload() {
  if (!kbUploadFile.value) {
    ElMessage.warning('请先选择 .md 文件')
    return
  }
  try {
    await kbApi.uploadDocument(kbUploadFile.value)
    ElMessage.success('上传成功，请重建向量库后生效')
    kbUploadFile.value = null
    loadKbDocuments()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function doKbDelete(doc) {
  try {
    await ElMessageBox.confirm(
      `确认删除「${doc.filename}」？删除后需重建向量库才彻底移除。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await kbApi.deleteDocument(doc.path)
    ElMessage.success('已删除')
    loadKbDocuments()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function doKbRebuild() {
  try {
    await ElMessageBox.confirm(
      '重建向量库会重新分块并 embedding 全部语料（首次可能耗时较长）。确认继续？',
      '重建确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  kbRebuilding.value = true
  try {
    await kbApi.rebuild()
    ElMessage.success('已触发重建，正在后台执行…')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    kbRebuilding.value = false
  }
}

function kbSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

onMounted(loadKbDocuments)
</script>

<template>
  <div class="kb-wrap">
    <div class="card">
      <div class="changes-head">
        <h3 class="card-title">知识库文档管理</h3>
        <el-button type="primary" plain :loading="kbRebuilding" @click="doKbRebuild">
          重建向量库
        </el-button>
      </div>
      <p class="rag-tip">
        管理知识库语料（canonical 下的 Markdown + FAQ JSON）。上传/删除文档后需「重建向量库」才会进入检索。
        上传文件名需符合 <code>&lt;id&gt;_&lt;标题&gt;.md</code>（id 形如 A-001，且已在 seeds 中登记）。
      </p>

      <div class="kb-upload">
        <el-upload
          :auto-upload="false"
          :limit="1"
          accept=".md"
          :on-change="onKbFileChange"
          :on-remove="() => (kbUploadFile = null)"
        >
          <el-button>选择 .md 文件</el-button>
        </el-upload>
        <el-button type="primary" :disabled="!kbUploadFile" @click="doKbUpload">上传</el-button>
      </div>

      <el-table v-loading="kbLoading" :data="kbDocuments" border stripe>
        <el-table-column prop="filename" label="文件名" min-width="260" show-overflow-tooltip />
        <el-table-column prop="category" label="分类" width="140" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'md' ? 'primary' : 'success'">
              {{ row.kind === 'md' ? 'MD' : 'FAQ' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ kbSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="doKbDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="kb-pagination">
        <el-pagination
          v-model:current-page="kbPage"
          :page-size="kbPageSize"
          :total="kbTotal"
          layout="total, prev, pager, next, jumper"
          background
          @current-change="onKbPageChange"
        />
      </div>
    </div>
  </div>
</template>

<style>
.kb-wrap {
  max-width: 1100px;
}
.kb-upload {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
}
.kb-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
