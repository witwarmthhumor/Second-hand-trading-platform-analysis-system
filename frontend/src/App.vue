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
import {
  DataBoard,
  DocumentChecked,
  Plus,
  TrendCharts,
  ChatDotRound,
  Bell,
  Collection,
  DataLine,
  User as UserIcon,
  Setting,
  Check,
  MoreFilled,
  DataAnalysis,
} from '@element-plus/icons-vue'
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
const FeedbackPanel = defineAsyncComponent(() => import('./components/FeedbackPanel.vue'))
const UserManage = defineAsyncComponent(() => import('./components/UserManage.vue'))
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
  // 换号登录同样清一次（防 401 过期路径已残留的状态带进新会话）
  resetSessionState()
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

/** 退出/过期共用的会话状态清理：store 与问答组件内部状态必须归零，
 * 否则下一个账号会看到上一个账号的对比勾选与表单残留（质量审计 P1）。 */
function resetSessionState() {
  resetStore()
  // 问答组件用 key 强制重建（见 template 里 RagChat 的 :key）
  ragChatKey.value++
  activeTab.value = 'list'
}

function doLogout() {
  clearAuth()
  currentUser.value = null
  resetSessionState()
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
  if (pwdNew.value.length < 8 || !/(?=.*[A-Za-z])(?=.*\d)/.test(pwdNew.value)) {
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

// —— 导航：分组的侧边栏（图标用 Element 官方线性图标，统一 16px 描边风格）——
const activeTab = ref('list')
const menuSections = [
  {
    title: '竞品情报',
    items: [
      { name: 'list', label: '竞品列表', icon: DataBoard },
      { name: 'pending', label: '待校对', icon: DocumentChecked },
      { name: 'form', label: '录入竞品', icon: Plus },
      { name: 'compare', label: '对比', icon: TrendCharts },
      { name: 'changes', label: '变化记录', icon: Bell },
    ],
  },
  {
    title: '知识库',
    items: [{ name: 'rag', label: '知识库问答', icon: ChatDotRound }],
  },
  {
    title: '系统管理',
    items: [
      { name: 'kb', label: '知识库管理', icon: Collection, adminOnly: true },
      { name: 'feedback', label: '反馈管理', icon: DataLine, adminOnly: true },
      { name: 'users', label: '账号管理', icon: UserIcon, adminOnly: true },
      { name: 'settings', label: '设置', icon: Setting },
    ],
  },
]
// 可见菜单：普通用户过滤掉 adminOnly 项，空分组整体隐藏
const visibleMenu = computed(() =>
  menuSections
    .map((sec) => ({ ...sec, items: sec.items.filter((m) => !m.adminOnly || isAdmin.value) }))
    .filter((sec) => sec.items.length > 0),
)
// 当前页面的标题/副标题（顶部面包屑用）
const pageMeta = {
  list: ['竞品列表', '竞品情报收集、整理与横向对比'],
  pending: ['待校对', 'AI 提炼结果人工审核'],
  form: ['录入竞品', '手动新增竞品信息'],
  compare: ['多竞品对比', '评分维度横向雷达对比'],
  rag: ['知识库问答', '融合 RAG 的智能问答助手'],
  changes: ['变化记录', '竞品信息变更历史追踪'],
  kb: ['知识库管理', '知识库语料维护（管理员专属）'],
  feedback: ['反馈管理', '好评 / 差评反馈回流分析（管理员专属）'],
  users: ['账号管理', '用户账号增删改查（管理员专属）'],
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
  // 数据广播由表单自己调 store.notifyChanged()，这里只负责回到列表
  activeTab.value = 'list'
}

function onFormCancel() {
  activeTab.value = 'list'
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
    // 与 doLogout 同一套清理：过期后换号登录不得继承前账号状态
    resetSessionState()
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
  <!-- 未登录：登录/注册视图（分屏：左侧品牌区 / 右侧表单） -->
  <div v-if="!isLoggedIn" class="auth-wrap">
    <aside class="auth-side">
      <div class="auth-side-brand">
        <span class="brand-mark"><el-icon :size="16"><DataAnalysis /></el-icon></span>
        <span class="brand-name">竞品分析平台</span>
      </div>
      <h2 class="auth-side-title">竞品情报与知识库问答<br />一站式分析工作台</h2>
      <ul class="auth-side-points">
        <li><el-icon><Check /></el-icon>竞品信息统一采集、校对与追踪</li>
        <li><el-icon><Check /></el-icon>多维度对比与一键导出分析报告</li>
        <li><el-icon><Check /></el-icon>基于知识库的智能问答与来源追溯</li>
      </ul>
      <div class="auth-side-foot">© 2026 二手交易平台分析系统</div>
    </aside>
    <div class="auth-panel">
    <div class="auth-card">
      <div class="auth-brand">
        <h1>{{ authMode === 'login' ? '登录工作台' : '创建账号' }}</h1>
        <p class="sub">{{ authMode === 'login' ? '使用平台账号继续' : '注册后即可开始使用' }}</p>
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
          还没有账号？<el-link type="primary" @click="switchAuthMode('register')">创建账号</el-link>
        </span>
        <span v-else>
          已有账号？<el-link type="primary" @click="switchAuthMode('login')">返回登录</el-link>
        </span>
      </div>
    </div>
    </div>
  </div>

  <!-- 已登录：主界面 -->
  <div v-else class="app">
    <!-- 左侧导航栏 -->
    <aside class="sidebar">
      <div class="sidebar-logo">
        <span class="sidebar-logo-icon"><el-icon :size="15"><DataAnalysis /></el-icon></span>
        <span class="sidebar-logo-text">竞品分析平台</span>
      </div>
      <nav class="sidebar-menu">
        <template v-for="sec in visibleMenu" :key="sec.title">
          <div class="sidebar-sec">{{ sec.title }}</div>
          <div
            v-for="m in sec.items"
            :key="m.name"
            class="sidebar-item"
            :class="{ active: activeTab === m.name }"
            @click="activeTab = m.name"
          >
            <el-icon class="sidebar-icon" :size="16"><component :is="m.icon" /></el-icon>
            <span class="sidebar-label">{{ m.label }}</span>
            <span v-if="m.name === 'pending' && pendingCount" class="sidebar-badge">
              {{ pendingCount }}
            </span>
            <span v-else-if="m.adminOnly" class="sidebar-admin">管理</span>
          </div>
        </template>
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
          <el-icon class="sidebar-more" :size="16"><MoreFilled /></el-icon>
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
          <span
            class="kb-chip"
            :class="{ ok: ragReady === true, warn: ragReady === false }"
          >
            <i class="dot" />{{
              ragReady === true
                ? '知识库就绪'
                : ragReady === false
                  ? '知识库未就绪'
                  : '知识库检测中'
            }}
          </span>
          <span v-if="isAdmin" class="role-chip">管理员</span>
        </div>
      </header>

      <main class="main">
        <!-- 竞品列表：数据变更由 store 广播，本组件不再向上抛事件 -->
        <CompetitorList v-if="activeTab === 'list'" @navigate="onNavigate" />

        <!-- 待校对 -->
        <PendingReview v-else-if="activeTab === 'pending'" @navigate="onNavigate" />

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

        <!-- 反馈管理（admin 专属） -->
        <FeedbackPanel v-else-if="activeTab === 'feedback'" />

        <!-- 账号管理（admin 专属） -->
        <UserManage v-else-if="activeTab === 'users'" />

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
/* 壳层样式：只保留 App 自身的布局（侧栏 / 顶栏 / 登录页）。
   跨页面共享类（.card / .toolbar / .section 等）已移至 styles/global.css。 */

/* ===== 主布局 ===== */
.app {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

/* ===== 侧边栏（石板深色 + 分组导航） ===== */
.sidebar {
  width: var(--sidebar-width);
  flex-shrink: 0;
  background: var(--sidebar-bg);
  display: flex;
  flex-direction: column;
}
.sidebar-logo {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  color: #f8fafc;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.3px;
  white-space: nowrap;
}
.sidebar-logo-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--brand-600);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sidebar-logo-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  padding: 2px 12px 12px;
}
.sidebar-sec {
  margin: 16px 10px 6px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 1.5px;
  color: #55637a;
}
.sidebar-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 38px;
  padding: 0 10px;
  margin: 2px 0;
  border-radius: 8px;
  cursor: pointer;
  color: var(--ink-sidebar);
  font-size: 13.5px;
  transition: background 0.15s, color 0.15s;
  white-space: nowrap;
}
.sidebar-item:hover {
  background: rgba(148, 163, 184, 0.09);
  color: #e2e8f0;
}
.sidebar-item.active {
  background: rgba(99, 102, 241, 0.16);
  color: #c7d2fe;
}
.sidebar-item.active::before {
  content: '';
  position: absolute;
  left: -12px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 2px 2px 0;
  background: var(--brand-500);
}
.sidebar-icon {
  flex-shrink: 0;
}
.sidebar-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-badge {
  margin-left: auto;
  background: #dc2626;
  color: #fff;
  font-size: 11px;
  line-height: 1;
  padding: 3px 7px;
  border-radius: 999px;
  font-variant-numeric: tabular-nums;
}
.sidebar-admin {
  margin-left: auto;
  font-size: 10px;
  line-height: 1.4;
  color: #818cf8;
  border: 1px solid rgba(99, 102, 241, 0.4);
  border-radius: 4px;
  padding: 1px 5px;
  letter-spacing: 1px;
}
.sidebar-footer {
  border-top: 1px solid rgba(148, 163, 184, 0.12);
  padding: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.sidebar-avatar {
  background: var(--brand-600);
  color: #fff;
  font-weight: 600;
  flex-shrink: 0;
}
.sidebar-user {
  flex: 1;
  min-width: 0;
}
.sidebar-user-name {
  color: #f1f5f9;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-user-role {
  font-size: 11px;
  color: #64748b;
}
.sidebar-more {
  color: #64748b;
  cursor: pointer;
  padding: 5px;
  border-radius: 6px;
  outline: none;
  transition: background 0.15s, color 0.15s;
}
.sidebar-more:hover {
  color: #e2e8f0;
  background: rgba(148, 163, 184, 0.09);
}

/* ===== 主区域 ===== */
.main-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.topbar {
  height: 56px;
  background: var(--surface);
  border-bottom: 1px solid var(--line-1);
  display: flex;
  align-items: center;
  padding: 0 var(--page-pad);
  flex-shrink: 0;
}
.topbar-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.topbar-crumb {
  font-size: 15px;
  font-weight: 600;
  color: var(--ink-1);
  letter-spacing: 0.2px;
  white-space: nowrap;
}
.topbar-sub {
  font-size: 12px;
  color: var(--ink-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
/* 知识库状态：胶囊 chip（琥珀=未就绪 / 绿=就绪） */
.kb-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  line-height: 1;
  padding: 5px 10px;
  border-radius: 999px;
  color: var(--ink-3);
  background: var(--el-fill-color-light);
  border: 1px solid var(--line-1);
  white-space: nowrap;
}
.kb-chip.warn {
  color: var(--el-color-warning-dark-2);
  background: var(--el-color-warning-light-9);
  border-color: var(--el-color-warning-light-7);
}
.kb-chip.ok {
  color: var(--el-color-success-dark-2);
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success-light-7);
}
.kb-chip .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
/* 角色徽标：中性品牌底，去橙色大标签 */
.role-chip {
  font-size: 12px;
  line-height: 1;
  padding: 5px 10px;
  border-radius: 999px;
  color: var(--brand-700);
  background: var(--brand-50);
  border: 1px solid var(--brand-100);
  white-space: nowrap;
}
.main {
  flex: 1;
  overflow-y: auto;
  padding: var(--page-pad);
}

/* ===== 登录页（分屏：左品牌 / 右表单） ===== */
.auth-wrap {
  min-height: 100vh;
  display: flex;
  background: var(--surface);
}
.auth-side {
  width: 44%;
  min-width: 400px;
  background: var(--sidebar-bg);
  color: #e2e8f0;
  padding: 44px 48px;
  display: flex;
  flex-direction: column;
}
.auth-side-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #f8fafc;
}
.brand-mark {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--brand-600);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.brand-name {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.3px;
}
.auth-side-title {
  margin: 0;
  padding-top: 72px;
  font-size: 24px;
  line-height: 1.5;
  font-weight: 600;
  letter-spacing: 0.3px;
  color: #f8fafc;
}
.auth-side-points {
  list-style: none;
  margin: 28px 0 0;
  padding: 0;
  display: grid;
  gap: 16px;
}
.auth-side-points li {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #94a3b8;
  font-size: 13.5px;
}
.auth-side-points li .el-icon {
  color: var(--brand-500);
  background: rgba(99, 102, 241, 0.14);
  border-radius: 50%;
  padding: 3px;
  box-sizing: content-box;
}
.auth-side-foot {
  margin-top: auto;
  font-size: 12px;
  color: #475569;
}
.auth-panel {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
}
.auth-card {
  width: 360px;
}
.auth-brand {
  margin-bottom: 28px;
}
.auth-brand h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0.3px;
  color: var(--ink-1);
}
.auth-brand .sub {
  margin: 8px 0 0;
  color: var(--ink-3);
  font-size: 13px;
}
.auth-submit {
  width: 100%;
  margin-top: 4px;
}
.auth-switch {
  margin-top: 20px;
  text-align: center;
  color: var(--ink-3);
  font-size: 13px;
}
@media (max-width: 860px) {
  .auth-side {
    display: none;
  }
}
</style>
