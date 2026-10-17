<script setup>
/**
 * 账号管理面板（仅管理员）：对全部用户做增删改查。
 *
 * 约束（与后端 /api/users 的守卫一致，这里在前端也提前拦一遍减少无谓请求）：
 * - 不能删除当前登录账号自己（按钮置灰 + 后端 409 双保险）；
 * - 不能删除/降级最后一个管理员（后端 409，前端把报错原样展示）；
 * - 编辑时密码留空 = 不改密码（payload 里不带该字段）。
 */
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete as DeleteIcon, Plus as PlusIcon } from '@element-plus/icons-vue'
import { userApi, getStoredUser } from '../api'
import { fmtTime } from '../utils/format'

const me = getStoredUser()
const loading = ref(false)
const users = ref([])
const selected = ref([]) // 多选行（批量删除用；自己的行不可勾选）

// —— 弹窗表单状态 ——
const dialogVisible = ref(false)
const editing = ref(null) // null = 新增；否则是被编辑的用户对象
const form = ref({ username: '', password: '', role: 'user' })
const submitting = ref(false)

const dialogTitle = computed(() => (editing.value ? `编辑用户：${editing.value.username}` : '新增用户'))

async function loadList() {
  loading.value = true
  try {
    users.value = await userApi.list()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = { username: '', password: '', role: 'user' }
  dialogVisible.value = true
}

/** 当前登录账号能否被勾选（批量删除不收自己——后端也会整批拒绝）。 */
function selectableRow(row) {
  return !(me && row.id === me.id)
}

const deleting = ref(false)

async function removeSelected() {
  const ids = selected.value.map((u) => u.id)
  if (!ids.length || deleting.value) return

  // 确认信息做截断与构成提示：选中多了不把弹窗撑爆，管理员数单独点名
  //（「会删光管理员」的后端守卫最常踩的就是这条）。
  const names = selected.value.map((u) => u.username)
  const shown = names.slice(0, 3).join('、') + (names.length > 3 ? ` 等 ${names.length} 个账号` : '')
  const adminCount = selected.value.filter((u) => u.role === 'admin').length
  const mix = adminCount
    ? `含 ${adminCount} 个管理员账号（从属数据一并清理；系统要求至少保留一名管理员）`
    : '删除后这些账号将无法登录，其会话/反馈等从属数据一并清理。'
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${shown}？${mix}`, '批量删除确认', {
      type: 'warning',
      confirmButtonText: '全部删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  deleting.value = true
  try {
    const r = await userApi.batchDelete(ids)
    ElMessage.success(`已删除 ${r.deleted} 个账号`)
    await loadList()
  } catch (e) {
    // 后端守卫（含自己/含不存在 id/会删光管理员）的 404/409 文案原样展示
    ElMessage.error(e.message)
  } finally {
    deleting.value = false
  }
}

function openEdit(user) {
  editing.value = user
  // 密码永远不回填（后端也不出参），留空 = 本次不改密码
  form.value = { username: user.username, password: '', role: user.role }
  dialogVisible.value = true
}

async function submitForm() {
  if (!form.value.username || form.value.username.length < 3) {
    ElMessage.warning('用户名至少 3 个字符（字母/数字/下划线/连字符）')
    return
  }
  if (!editing.value && !form.value.password) {
    ElMessage.warning('新增用户必须设置初始密码')
    return
  }
  if (form.value.password && form.value.password.length < 8) {
    ElMessage.warning('密码至少 8 位（字母+数字，不能是常见弱口令）')
    return
  }

  submitting.value = true
  try {
    if (editing.value) {
      const payload = { username: form.value.username, role: form.value.role }
      if (form.value.password) payload.password = form.value.password
      await userApi.update(editing.value.id, payload)
      ElMessage.success('已更新')
    } else {
      await userApi.create({
        username: form.value.username,
        password: form.value.password,
        role: form.value.role,
      })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await loadList()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    submitting.value = false
  }
}

async function removeUser(user) {
  const isSelf = me && user.id === me.id
  const hint = isSelf
    ? '不能删除当前登录的账号。'
    : user.role === 'admin'
      ? '这是管理员账号，删除其会话/反馈等从属数据将一并清理。'
      : '删除后该账号将无法登录，其会话/反馈等从属数据一并清理。'
  try {
    await ElMessageBox.confirm(`确定删除用户「${user.username}」？${hint}`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  try {
    await userApi.remove(user.id)
    ElMessage.success('已删除')
    await loadList()
  } catch (e) {
    // 后端守卫（删自己 / 最后一个 admin）的 409 文案原样展示
    ElMessage.error(e.message)
  }
}

onMounted(loadList)
</script>

<template>
  <div class="users-wrap">
    <div class="card">
      <div class="changes-head">
        <h3 class="card-title">账号管理</h3>
        <div class="head-actions">
          <el-button
            type="danger"
            plain
            :disabled="!selected.length"
            :loading="deleting"
            @click="removeSelected"
          >
            批量删除{{ selected.length ? `（${selected.length}）` : '' }}
          </el-button>
          <el-button type="primary" @click="openCreate"><el-icon><PlusIcon /></el-icon>&nbsp;新增用户</el-button>
        </div>
      </div>
      <p class="rag-tip">
        管理全部用户账号（创建 / 改名 / 重置密码 / 角色调整 / 删除）。编辑时密码留空表示不修改；
        系统始终要求至少保留一名管理员，且不能删除当前登录的账号。
      </p>

      <el-table
        :data="users"
        v-loading="loading"
        stripe
        @selection-change="(rows) => (selected = rows)"
      >
        <el-table-column type="selection" width="45" :selectable="selectableRow" />
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="username" label="用户名" min-width="160" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'warning' : 'info'">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              type="danger"
              :disabled="me && row.id === me.id"
              @click="removeUser(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="440px">
      <el-form label-width="80px" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="3~64 位：字母/数字/下划线/连字符" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="editing ? '留空表示不修改密码' : '至少 8 位，字母+数字'"
          />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="form.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForm">
          {{ editing ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.users-wrap {
  padding: 0;
}
.head-actions {
  display: flex;
  gap: 10px;
}
</style>
