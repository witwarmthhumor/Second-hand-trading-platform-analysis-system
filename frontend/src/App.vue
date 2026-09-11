<script setup>
/**
 * 应用壳层：登录/注册、左侧导航、顶栏，以及各功能页的装配与切换。
 *
 * 竞品侧 7 个 tab 已拆为独立组件（M1），本文件只保留：
 * - 登录态与改密
 * - 导航与页面标题
 * - 跨组件共享状态：评分维度、待校对数量、当前编辑对象、对比选中项
 * - 由组件事件驱动的页面跳转
 *
 * 各页组件均为 defineAsyncComponent 懒加载：进入对应 tab 时才拉取其代码块与数据。
 */
import { ref, onMounted, computed, defineAsyncComponent } from 'vue'
import { ElMessage } from 'element-plus'
import { competitorApi, authApi, setAuth, clearAuth, getStoredUser } from './api'
import { useCompetitorStore } from './stores/competitor'

// 竞品侧功能页（懒加载）
const CompetitorList = defineAsyncComponent(() => import('./components/CompetitorList.vue'))
const PendingReview = defineAsyncComponent(() => import('./components/PendingReview.vue'))
const CompetitorForm = defineAsyncComponent(() => import('./components/CompetitorForm.vue'))
const ComparePanel = defineAsyncComponent(() => import('./components/ComparePanel.vue'))
const ChangesPanel = defineAsyncComponent(() => import('./components/ChangesPanel.vue'))
const KbManage = defineAsyncComponent(() => import('./components/KbManage.vue'))
const SettingsPanel = defineAsyncComponent(() => import('./components/SettingsPanel.vue'))
// 知识库问答（自持会话、来源、反馈等全部状态）
const RagChat = defineAsyncComponent(() => import('./components/RagChat.vue'))

// —— 登录态 ——
const currentUser = ref(getStoredUser())
const isLoggedIn = computed(() => !!currentUser.value)
const isAdmin = computed(() => currentUser.value?.role === 'admin')

const authMode = ref('login') // 'login' | 'register'
const authUsername = ref('')
const authPassword = ref('')
const authConfirm = ref('')
const authLoading = ref(false)

function enterApp(token, user) {
  setAuth(token, user)
  currentUser.value = user
  activeTab.value = 'list'
  // 登录后初始化壳层级数据（各功能页数据由其组件自行加载）
  initShellData()
  loadRagStatus()
}

async function doLogin() {
  if (!authUsername.value || !authPassword.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  authLoading.value = true
  try {
    const r = await authApi.login(authUsername.value, authPassword.value)
    enterApp(r.access_token, r.user)
    ElMessage.success('登录成功')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    authLoading.value = false
  }
}

async function doRegister() {
  if (!authUsername.value || !authPassword.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  if (authPassword.value !== authConfirm.value) {
    ElMessage.warning('两次密码不一致')
    return
  }
  authLoading.value = true
  try {
    const r = await authApi.register(authUsername.value, authPassword.value)
    enterApp(r.access_token, r.user)
    ElMessage.success('注册成功，已自动登录')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    authLoading.value = false
  }
}

function switchAuthMode(mode) {
  authMode.value = mode
  authConfirm.value = ''
}

function doLogout() {
  clearAuth()
  currentUser.value = null
  activeTab.value = 'list'
  // 清空共享状态与问答组件内部状态，避免下一个账号看到上一个账号的残留数据
  resetStore()
  // 问答组件用 key 强制重建（见 template 里 RagChat 的 :key）
  ragChatKey.value++
  ElMessage.success('已退出登录')
}

// 改密对话框
const pwdDialog = ref(false)
const pwdOld = ref('')
const pwdNew = ref('')
const pwdConfirm = ref('')
const pwdSaving = ref(false)

function openChangePwd() {
  pwdOld.value = ''
  pwdNew.value = ''
  pwdConfirm.value = ''
  pwdDialog.value = true
}

async function doChangePwd() {
  if (pwdNew.value.length < 8) {
    ElMessage.warning('新密码至少 8 位，且需同时包含字母与数字')
    return
  }
  if (pwdNew.value !== pwdConfirm.value) {
    ElMessage.warning('两次新密码不一致')
    return
  }
  pwdSaving.value = true
  try {
    await authApi.changePassword(pwdOld.value, pwdNew.value)
    ElMessage.success('密码已修改，下次登录请使用新密码')
    pwdDialog.value = false
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    pwdSaving.value = false
  }
}

// —— 导航 ——
const activeTab = ref('list')
const menuItems = [
  { name: 'list', label: '竞品列表', icon: '📊' },
  { name: 'pending', label: '待校对', icon: '📝' },
  { name: 'form', label: '录入竞品', icon: '➕' },
  { name: 'compare', label: '对比', icon: '⚖️' },
  { name: 'rag', label: '知识库问答', icon: '💬' },
  { name: 'changes', label: '变化记录', icon: '🔔' },
  { name: 'kb', label: '知识库管理', icon: '📚', adminOnly: true },
  { name: 'settings', label: '设置', icon: '⚙️' },
]
// 可见菜单：普通用户过滤掉 adminOnly
const visibleMenu = computed(() => menuItems.filter((m) => !m.adminOnly || isAdmin.value))
// 当前页面的标题/副标题（顶部面包屑用）
const pageMeta = {
  list: ['竞品列表', '竞品情报收集、整理与横向对比'],
  pending: ['待校对', 'AI 提炼结果人工审核'],
  form: ['录入竞品', '手动新增竞品信息'],
  compare: ['多竞品对比', '评分维度横向雷达对比'],
  rag: ['知识库问答', '融合 RAG 的智能问答助手'],
  changes: ['变化记录', '竞品信息变更历史追踪'],
  kb: ['知识库管理', '知识库语料维护（管理员专属）'],
  settings: ['设置', '评分维度与系统配置'],
}
const currentTitle = computed(() => pageMeta[activeTab.value]?.[0] || '')
const currentSub = computed(() => pageMeta[activeTab.value]?.[1] || '')

// —— 跨组件共享状态 ——
// 待校对数量与评分维度已收敛到 stores/competitor.js，组件直接从 store 读写，
// 不再经 App 中转；此处只保留壳层自用部分（侧栏徽标计数）。
const {
  pendingCount,
  formKey,
  loadPending,
  loadRatingDims,
  reset: resetStore,
} = useCompetitorStore()

/** 壳层初始化数据（各功能页的数据由其组件自行加载）。 */
function initShellData() {
  loadPending()
  // 维度加载失败需要提示，否则录入表单的评分项会静默消失
  loadRatingDims().catch((e) => ElMessage.error(e.message))
}

// —— 导航：子组件只表达「去哪一页」，数据状态一律走 store ——
function onNavigate(tab) {
  activeTab.value = tab
}

function onFormSaved() {
  // 列表页在重新挂载时会自取最新数据，这里只需回到列表
  activeTab.value = 'list'
  loadPending()
}

function onFormCancel() {
  activeTab.value = 'list'
}

// 列表/待校对页发生增删后：刷新待校对数据（侧栏徽标计数由它派生）
function onDataChanged() {
  loadPending()
}

// —— RAG 知识库就绪状态（顶栏显示用；问答组件 RagChat 自持其余状态）——
const ragReady = ref(null) // null=未知, true/false
// 问答组件重建 key：退出登录时自增，强制重建 RagChat 以清空其内部会话状态
const ragChatKey = ref(0)

async function loadRagStatus() {
  try {
    const r = await competitorApi.ragStatus()
    ragReady.value = r.ready
  } catch {
    ragReady.value = false
  }
}

// 登录态失效监听（401 触发）
onMounted(() => {
  window.addEventListener('auth-expired', () => {
    currentUser.value = null
    ElMessage.warning('登录已过期，请重新登录')
  })
  if (isLoggedIn.value) {
    // 已有 token，直接初始化壳层级数据
    initShellData()
    loadRagStatus()
  }
})
</script>

<template>
  <!-- 未登录：登录/注册视图 -->
  <div v-if="!isLoggedIn" class="auth-wrap">
    <div class="auth-card">
      <div class="auth-brand">
        <h1>二手交易平台分析系统</h1>
        <p class="sub">竞品情报 · 知识库问答</p>
      </div>
      <el-form @submit.prevent>
        <el-form-item>
          <el-input v-model="authUsername" placeholder="用户名" size="large" clearable />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="authPassword"
            type="password"
            placeholder="密码"
            size="large"
            show-password
            @keyup.enter="authMode === 'login' ? doLogin() : doRegister()"
          />
        </el-form-item>
        <el-form-item v-if="authMode === 'register'">
          <el-input
            v-model="authConfirm"
            type="password"
            placeholder="确认密码"
            size="large"
            show-password
            @keyup.enter="doRegister"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          class="auth-submit"
          :loading="authLoading"
          @click="authMode === 'login' ? doLogin() : doRegister()"
        >
          {{ authMode === 'login' ? '登 录' : '注 册' }}
        </el-button>
      </el-form>
      <div class="auth-switch">
        <span v-if="authMode === 'login'">
          还没有账号？<el-link type="primary" @click="switchAuthMode('register')">去注册</el-link>
        </span>
        <span v-else>
          已有账号？<el-link type="primary" @click="switchAuthMode('login')">去登录</el-link>
        </span>
      </div>
    </div>
  </div>

  <!-- 已登录：主界面 -->
  <div v-else class="app">
    <!-- 左侧导航栏 -->
    <aside class="sidebar">
      <div class="sidebar-logo">
        <span class="sidebar-logo-icon">⇄</span>
        <span class="sidebar-logo-text">二手交易平台分析</span>
      </div>
      <nav class="sidebar-menu">
        <div
          v-for="m in visibleMenu"
          :key="m.name"
          class="sidebar-item"
          :class="{ active: activeTab === m.name }"
          @click="activeTab = m.name"
        >
          <span class="sidebar-icon">{{ m.icon }}</span>
          <span class="sidebar-label">{{ m.label }}</span>
          <span v-if="m.name === 'pending' && pendingCount" class="sidebar-badge">
            {{ pendingCount }}
          </span>
          <span v-else-if="m.adminOnly" class="sidebar-admin">admin</span>
        </div>
      </nav>
      <div class="sidebar-footer">
        <el-avatar :size="32" class="sidebar-avatar">
          {{ (currentUser?.username || '?').slice(0, 1).toUpperCase() }}
        </el-avatar>
        <div class="sidebar-user">
          <div class="sidebar-user-name">{{ currentUser?.username }}</div>
          <div class="sidebar-user-role">{{ isAdmin ? '管理员' : '普通用户' }}</div>
        </div>
        <el-dropdown trigger="click">
          <span class="sidebar-more">···</span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="openChangePwd">修改密码</el-dropdown-item>
              <el-dropdown-item divided @click="doLogout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </aside>

    <!-- 主区域 -->
    <div class="main-wrap">
      <header class="topbar">
        <div class="topbar-title">
          <span class="topbar-crumb">{{ currentTitle }}</span>
          <span class="topbar-sub">{{ currentSub }}</span>
        </div>
        <div class="topbar-right">
          <span class="rag-status" :class="{ ready: ragReady !== false }">
            <i class="dot" /> {{ ragReady === false ? '知识库未就绪' : '知识库就绪' }}
          </span>
          <el-tag v-if="isAdmin" type="warning" size="small" effect="dark">管理员</el-tag>
        </div>
      </header>

      <main class="main">
        <!-- 竞品列表 -->
        <CompetitorList
          v-if="activeTab === 'list'"
          @navigate="onNavigate"
          @changed="onDataChanged"
        />

        <!-- 待校对 -->
        <PendingReview
          v-else-if="activeTab === 'pending'"
          @navigate="onNavigate"
          @changed="onDataChanged"
        />

        <!-- 录入 / 编辑：待编辑对象与评分维度均来自 store，key 变化即强制重建表单 -->
        <CompetitorForm
          v-else-if="activeTab === 'form'"
          :key="formKey"
          @saved="onFormSaved"
          @cancel="onFormCancel"
        />

        <!-- 多竞品对比：对比选中项来自 store -->
        <ComparePanel v-else-if="activeTab === 'compare'" @navigate="onNavigate" />

        <!-- 知识库问答（懒加载组件） -->
        <RagChat v-else-if="activeTab === 'rag'" :key="ragChatKey" :ready="ragReady" />

        <!-- 变化记录 -->
        <ChangesPanel v-else-if="activeTab === 'changes'" />

        <!-- 知识库管理（admin 专属） -->
        <KbManage v-else-if="activeTab === 'kb'" />

        <!-- 设置 -->
        <SettingsPanel v-else-if="activeTab === 'settings'" />

        <!-- 修改密码对话框 -->
        <el-dialog v-model="pwdDialog" title="修改密码" width="420px">
          <el-form @submit.prevent>
            <el-form-item label="旧密码">
              <el-input v-model="pwdOld" type="password" show-password />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input
                v-model="pwdNew"
                type="password"
                show-password
                placeholder="至少 8 位，含字母与数字"
              />
            </el-form-item>
            <el-form-item label="确认新密码">
              <el-input v-model="pwdConfirm" type="password" show-password />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="pwdDialog = false">取消</el-button>
            <el-button type="primary" :loading="pwdSaving" @click="doChangePwd">确认修改</el-button>
          </template>
        </el-dialog>
      </main>
    </div>
  </div>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
}
.app {
  display: flex;
  min-height: 100vh;
  height: 100vh;
  overflow: hidden;
}
/* ===== 侧边栏 ===== */
.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: #1d2b36;
  color: #bfcbd9;
  display: flex;
  flex-direction: column;
}
.sidebar-logo {
  height: 60px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  white-space: nowrap;
}
.sidebar-logo-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #409eff, #7b5cff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}
.sidebar-logo-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}
.sidebar-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  height: 44px;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 14px;
  border-left: 3px solid transparent;
  white-space: nowrap;
}
.sidebar-item:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #fff;
}
.sidebar-item.active {
  background: rgba(64, 158, 255, 0.15);
  color: #fff;
  border-left-color: #409eff;
}
.sidebar-icon {
  width: 18px;
  text-align: center;
  font-size: 15px;
}
.sidebar-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-badge {
  margin-left: auto;
  background: #f56c6c;
  color: #fff;
  font-size: 11px;
  border-radius: 10px;
  padding: 1px 7px;
}
.sidebar-admin {
  margin-left: auto;
  font-size: 10px;
  color: #e6a23c;
  border: 1px solid #e6a23c;
  border-radius: 3px;
  padding: 0 4px;
}
.sidebar-footer {
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.sidebar-avatar {
  background: linear-gradient(135deg, #409eff, #67c23a);
  flex-shrink: 0;
}
.sidebar-user {
  flex: 1;
  min-width: 0;
}
.sidebar-user-name {
  color: #fff;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-user-role {
  font-size: 11px;
  color: #8a9bb0;
}
.sidebar-more {
  color: #8a9bb0;
  cursor: pointer;
  font-size: 16px;
  padding: 0 6px;
  outline: none;
}
.sidebar-more:hover {
  color: #fff;
}
/* ===== 主区域 ===== */
.main-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.topbar {
  height: 60px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}
.topbar-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.topbar-crumb {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}
.topbar-sub {
  font-size: 12px;
  color: #909399;
}
.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rag-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #67c23a;
}
.rag-status .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.rag-status.ready {
  color: #67c23a;
}
.rag-status:not(.ready) {
  color: #e6a23c;
}
.sub {
  margin: 2px 0 8px;
  color: #909399;
  font-size: 12px;
}
.main {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
/* ===== 多个功能页共用的基础样式 ===== */
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.toolbar-spacer {
  flex: 1;
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
.rag-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.rag-head .card-title {
  margin: 0;
}
.changes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.changes-head .card-title {
  margin: 0;
}
.rag-tip {
  color: #909399;
  font-size: 13px;
  margin: 12px 0 16px;
  line-height: 1.6;
}
/* ===== 登录页 ===== */
.auth-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.auth-card {
  width: 380px;
  background: #fff;
  border-radius: 10px;
  padding: 40px 36px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.auth-brand {
  text-align: center;
  margin-bottom: 24px;
}
.auth-brand h1 {
  margin: 0;
  font-size: 20px;
  color: #303133;
}
.auth-brand .sub {
  margin: 6px 0 0;
}
.auth-submit {
  width: 100%;
}
.auth-switch {
  margin-top: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
</style>
