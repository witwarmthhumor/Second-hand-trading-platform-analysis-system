<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { competitorApi } from '../api'

// 知识库就绪状态由父组件（App.vue）加载并下发，本组件只消费
const props = defineProps({
  ready: { type: Boolean, default: null }, // null=未知, true/false
})

const ragQuestion = ref('')
const ragAnswer = ref(null) // { answer, mode, sources }
const ragLoading = ref(false)
const ragSessionId = ref(null) // 当前会话 ID（多轮记忆）
const ragHistory = ref([]) // 本轮会话的问答历史 [{ role, content, mode, sources, feedback }]
const ragSessions = ref([]) // 历史会话列表

async function loadRagSessions() {
  try {
    ragSessions.value = await competitorApi.ragListSessions()
  } catch (e) {
    ragSessions.value = []
  }
}

const chatInputEl = ref(null) // 输入框实例，供「新建对话」后聚焦

function newRagSession() {
  // 新对话：仅本地清空（不再立即落库）。首问时前端生成 session_id，
  // 后端 ask 懒创建会话，避免侧栏堆积「空会话」。
  ragSessionId.value = null
  ragHistory.value = []
  ragAnswer.value = null
  ragQuestion.value = '' // 清掉输入框里可能残留的上一条问题
  batchMode.value = false
  checkedSessions.value = []
  // 因为「懒创建」，点击后侧栏不会新增条目；若当前本来就是空对话，清空更无可见变化，
  // 会被误认为「点了没反应」。故这里给出确定反馈：聚焦输入框 + 一条提示。
  nextTick(() => chatInputEl.value?.focus())
  ElMessage.success('已开启新对话')
}

async function doAsk() {
  const q = ragQuestion.value.trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  ragLoading.value = true
  try {
    // 新对话的首问：本地生成 session_id，后端 ask 懒创建并归属当前用户
    if (!ragSessionId.value) {
      ragSessionId.value = crypto.randomUUID().replace(/-/g, '')
    }
    // 记录用户消息到本地历史
    ragHistory.value.push({ role: 'user', content: q })
    ragQuestion.value = ''
    scrollChatToBottom()

    // 流式问答：AI 消息边收边渲染
    const aiMsg = { role: 'assistant', content: '', sources: [], mode: 'fallback', feedback: null }
    ragHistory.value.push(aiMsg)
    let doneInfo = null
    for await (const evt of competitorApi.ragAskStream(q, 5, ragSessionId.value)) {
      if (evt.type === 'delta') {
        aiMsg.content += evt.content
        scrollChatToBottom()
      } else if (evt.type === 'done') {
        doneInfo = evt
        aiMsg.mode = evt.mode || 'fallback'
        if (evt.answer) aiMsg.content = evt.answer
        if (evt.sources) aiMsg.sources = evt.sources
      }
    }
    // 记录该 AI 回答对应的用户问题，供反馈回路使用
    aiMsg.question = q
    // 若流式异常结束且无内容，给出兜底
    if (!aiMsg.content && doneInfo?.answer) {
      aiMsg.content = doneInfo.answer
    }
    loadRagSessions()
    scrollChatToBottom()
  } catch (e) {
    ElMessage.error(e.message)
    // 移除空 AI 气泡（若尚未渲染出内容）
    const last = ragHistory.value[ragHistory.value.length - 1]
    if (last && last.role === 'assistant' && !last.content) {
      ragHistory.value.pop()
    }
  } finally {
    ragLoading.value = false
  }
}

// 聊天区自动滚动到底部
const chatMsgsEl = ref(null)
function scrollChatToBottom() {
  nextTick(() => {
    if (chatMsgsEl.value) chatMsgsEl.value.scrollTop = chatMsgsEl.value.scrollHeight
  })
}

async function copySourceText(s) {
  // 复制来源文本，供用户溯源核对
  try {
    await navigator.clipboard.writeText(`【${s.doc_id}】${s.title}\n${s.text}`)
    ElMessage.success('来源文本已复制')
  } catch (e) {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

// —— 问答反馈（👍/👎）——
async function submitFeedback(msg, rating) {
  // 未关联会话或问题，无法落库（通常不会发生）
  if (!ragSessionId.value || !msg.question) {
    ElMessage.warning('当前消息暂不支持反馈')
    return
  }
  try {
    await competitorApi.ragFeedback({
      session_id: ragSessionId.value,
      rating,
      question: msg.question,
      answer: msg.content,
      mode: msg.mode || 'fallback',
      source_doc_ids: (msg.sources || []).map((s) => s.doc_id),
    })
    msg.feedback = rating
    ElMessage.success(rating === 'like' ? '感谢反馈，已记录 👍' : '感谢反馈，已记录 👎')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// —— 会话侧栏 ——
// 会话时间显示：今天显示 HH:mm，否则显示 MM-DD
function fmtSessionTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  if (d.toDateString() === now.toDateString()) return hm
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

async function switchSession(sessionId) {
  ragSessionId.value = sessionId
  ragHistory.value = []
  ragAnswer.value = null
  try {
    // 从后端拉取该会话的完整消息历史并回显到对话区
    const detail = await competitorApi.ragGetSession(sessionId)
    // 消息表只存 role/content，不存「这条回答对应的提问」，因此回显时按顺序
    // 把每条 AI 回答与其上一条用户提问配成对，赋给 question 供 👍/👎 反馈落库；
    // 否则历史消息的反馈按钮会一律提示「当前消息暂不支持反馈」。
    let lastUserQuestion = ''
    ragHistory.value = (detail.messages || []).map((m) => {
      if (m.role === 'user') {
        lastUserQuestion = m.content
        return { role: 'user', content: m.content }
      }
      return {
        role: 'assistant',
        content: m.content,
        mode: m.mode || 'fallback',
        sources: m.sources || [],
        question: lastUserQuestion,
        // 反馈状态：历史消息默认未反馈
        feedback: null,
      }
    })
    scrollChatToBottom()
  } catch (e) {
    ElMessage.error('加载会话失败：' + e.message)
  }
}

// —— 批量删除 ——
const batchMode = ref(false) // 批量删除模式
const checkedSessions = ref([]) // 勾选的 session_id

function toggleBatchMode() {
  batchMode.value = !batchMode.value
  checkedSessions.value = []
}

function toggleSessionCheck(sessionId) {
  const i = checkedSessions.value.indexOf(sessionId)
  if (i >= 0) {
    checkedSessions.value.splice(i, 1)
  } else {
    checkedSessions.value.push(sessionId)
  }
}

const allChecked = computed(
  () => ragSessions.value.length > 0 && checkedSessions.value.length === ragSessions.value.length
)

function toggleCheckAll() {
  checkedSessions.value = allChecked.value ? [] : ragSessions.value.map((s) => s.session_id)
}

async function doBatchDelete() {
  if (!checkedSessions.value.length) {
    ElMessage.warning('请先勾选要删除的会话')
    return
  }
  try {
    await ElMessageBox.confirm(`确认删除选中的 ${checkedSessions.value.length} 个会话？`, '批量删除', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const r = await competitorApi.ragBatchDeleteSessions([...checkedSessions.value])
    ElMessage.success(`已删除 ${r.deleted} 个会话`)
    // 若当前会话被删除，清空对话区
    if (checkedSessions.value.includes(ragSessionId.value)) {
      ragSessionId.value = null
      ragHistory.value = []
      ragAnswer.value = null
    }
    checkedSessions.value = []
    batchMode.value = false
    loadRagSessions()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(() => {
  loadRagSessions()
})
</script>

<template>
  <div class="rag-wrap">
    <el-alert
      v-if="ready === false"
      title="知识库尚未就绪，请先在后端运行建库脚本（scripts/build_vector_store.py）生成向量库。"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    />
    <div class="chat-layout">
      <!-- 会话侧栏 -->
      <div class="rag-side">
        <div class="rag-side-actions">
          <el-button class="new-chat-btn" type="primary" plain @click="newRagSession">
            ＋ 新建对话
          </el-button>
          <el-tooltip :content="batchMode ? '退出批量删除' : '批量删除'" placement="top">
            <el-button
              class="batch-btn"
              :type="batchMode ? 'warning' : 'default'"
              text
              @click="toggleBatchMode"
            >
              🗑
            </el-button>
          </el-tooltip>
        </div>

        <!-- 批量模式工具条 -->
        <div v-if="batchMode" class="rag-side-batchbar">
          <el-checkbox :model-value="allChecked" @change="toggleCheckAll">全选</el-checkbox>
          <el-button size="small" type="danger" text @click="doBatchDelete">
            删除({{ checkedSessions.length }})
          </el-button>
        </div>

        <div class="rag-side-list">
          <div
            v-for="s in ragSessions"
            :key="s.session_id"
            class="rag-side-item"
            :class="{ active: !batchMode && s.session_id === ragSessionId }"
            @click="batchMode ? toggleSessionCheck(s.session_id) : switchSession(s.session_id)"
          >
            <el-checkbox
              v-if="batchMode"
              :model-value="checkedSessions.includes(s.session_id)"
              @change="toggleSessionCheck(s.session_id)"
              @click.stop
            />
            <div class="rag-side-item-body">
              <div class="rag-side-title" :title="s.first_question || '新对话'">
                {{ s.first_question || '新对话' }}
              </div>
              <div class="rag-side-meta">
                {{ s.message_count }} 条 · {{ fmtSessionTime(s.updated_at) }}
              </div>
            </div>
          </div>
          <div v-if="!ragSessions.length" class="rag-side-empty">暂无会话</div>
        </div>
      </div>

      <!-- 对话区：气泡流 -->
      <div class="chat-main">
        <div class="chat-msgs" ref="chatMsgsEl">
          <!-- 空会话：Right Code 式极简空状态 -->
          <div v-if="!ragHistory.length && !ragLoading" class="chat-welcome">
            <div class="chat-welcome-icon">
              <svg viewBox="0 0 24 24" width="30" height="30" fill="none">
                <path
                  d="M12 3C7.03 3 3 6.58 3 11c0 2.09.9 4 2.38 5.42-.17 1.08-.68 2.35-1.85 3.32-.24.2-.1.6.21.58 1.68-.12 3.13-.75 4.14-1.4 1.26.44 2.65.68 4.12.68 4.97 0 9-3.58 9-8s-4.03-8-9-8Z"
                  fill="#fff"
                />
                <circle cx="8.5" cy="11" r="1.1" fill="#7b5cff" />
                <circle cx="12" cy="11" r="1.1" fill="#7b5cff" />
                <circle cx="15.5" cy="11" r="1.1" fill="#7b5cff" />
              </svg>
            </div>
            <p class="chat-welcome-title">开始新的对话吧</p>
            <p class="chat-welcome-sub">
              支持多轮追问 · 命中知识库返回原文并标注来源，未命中由大模型兜底
            </p>
          </div>

          <!-- 消息流 -->
          <div
            v-for="(m, i) in ragHistory"
            :key="i"
            class="chat-msg"
            :class="m.role"
          >
            <div class="chat-avatar">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <div class="chat-body">
              <!-- AI 消息内容为空且加载中时显示打字点 -->
              <div v-if="m.role === 'assistant' && !m.content && ragLoading" class="chat-bubble chat-typing">
                <span class="typing-dot" /><span class="typing-dot" /><span class="typing-dot" />
              </div>
              <div v-else class="chat-bubble">{{ m.content }}</div>
              <!-- 反馈按钮：仅 AI 消息且有内容时显示 -->
              <div v-if="m.role === 'assistant' && m.content" class="chat-feedback">
                <span
                  class="fb-btn"
                  :class="{ active: m.feedback === 'like' }"
                  @click="submitFeedback(m, 'like')"
                >👍</span>
                <span
                  class="fb-btn"
                  :class="{ active: m.feedback === 'dislike' }"
                  @click="submitFeedback(m, 'dislike')"
                >👎</span>
              </div>
              <!-- AI 消息带来源引用 -->
              <div
                v-if="m.role === 'assistant' && m.sources && m.sources.length"
                class="chat-sources"
              >
                <el-collapse>
                  <el-collapse-item
                    v-for="(s, j) in m.sources"
                    :key="j"
                    :name="j"
                  >
                    <template #title>
                      <div class="rag-src-title">
                        <el-tag size="small" type="info">{{ s.doc_id }}</el-tag>
                        <b>{{ s.title }}</b>
                        <span class="rag-score">相关度 {{ (s.score * 100).toFixed(1) }}%</span>
                      </div>
                    </template>
                    <div class="rag-src-body">
                      <p>{{ s.text }}</p>
                      <el-button size="small" text type="primary" @click.stop="copySourceText(s)">
                        复制来源文本
                      </el-button>
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>
            </div>
          </div>
        </div>

        <!-- 底部输入框 -->
        <div class="chat-input-bar">
          <el-input
            ref="chatInputEl"
            v-model="ragQuestion"
            placeholder="输入问题，例如：忘记密码怎么办 / 卖家入驻需要什么资质"
            clearable
            @keyup.enter="doAsk"
          />
          <el-button type="primary" :loading="ragLoading" @click="doAsk">发送</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
.rag-wrap {
  /* 知识库问答页容器：内部高度由 chat-layout 计算 */
}
.rag-tip {
  color: #909399;
  font-size: 13px;
  margin: 0 0 16px;
  line-height: 1.6;
}
.rag-src-title {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.rag-src-title b {
  font-size: 13px;
  color: #303133;
}
.rag-src-body p {
  margin: 0 0 8px;
  color: #606266;
  background: #fafafa;
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.rag-score {
  margin-left: auto;
  color: #909399;
  font-size: 12px;
}
.chat-layout {
  display: flex;
  gap: 16px;
  align-items: stretch;
  height: calc(100vh - 60px - 40px - 12px);
  min-height: 480px;
}
.rag-side {
  width: 260px;
  flex-shrink: 0;
  background: #fff;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.rag-side-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.new-chat-btn {
  flex: 1;
}
.batch-btn {
  font-size: 16px;
  padding: 8px 10px;
}
.rag-side-batchbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  flex-shrink: 0;
}
.rag-side-list {
  flex: 1;
  overflow-y: auto;
}
.rag-side-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
}
.rag-side-item:hover {
  background: #f5f7fa;
}
.rag-side-item.active {
  background: #ecf5ff;
  border-color: #c6e2ff;
}
.rag-side-item-body {
  min-width: 0;
  flex: 1;
}
.rag-side-title {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rag-side-meta {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.rag-side-empty {
  text-align: center;
  color: #c0c4cc;
  font-size: 13px;
  padding: 32px 0;
}
.chat-main {
  flex: 1;
  min-width: 0;
  background: #fff;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.chat-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}
.chat-welcome {
  text-align: center;
  padding: 120px 20px 30px;
  max-width: 520px;
  margin: 0 auto;
}
.chat-welcome-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto 18px;
  border-radius: 18px;
  background: linear-gradient(135deg, #409eff, #7b5cff);
  display: flex;
  align-items: center;
  justify-content: center;
}
.chat-welcome-title {
  font-size: 15px;
  color: #909399;
  margin-bottom: 8px;
}
.chat-welcome-sub {
  font-size: 12px;
  color: #c0c4cc;
}
.chat-msg {
  display: flex;
  gap: 12px;
  margin-bottom: 22px;
}
.chat-msg.user {
  flex-direction: row-reverse;
}
.chat-avatar {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 13px;
}
.chat-msg.assistant .chat-avatar {
  background: linear-gradient(135deg, #409eff, #7b5cff);
}
.chat-msg.user .chat-avatar {
  background: linear-gradient(135deg, #67c23a, #409eff);
}
.chat-body {
  max-width: 72%;
  min-width: 0;
}
.chat-bubble {
  padding: 12px 16px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
  white-space: pre-wrap;
}
.chat-msg.assistant .chat-bubble {
  background: #f5f7fa;
  border-top-left-radius: 2px;
}
.chat-msg.user .chat-bubble {
  background: #409eff;
  color: #fff;
  border-top-right-radius: 2px;
}
.chat-sources {
  margin-top: 8px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fafbfc;
  overflow: hidden;
}
.chat-feedback {
  margin-top: 6px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.fb-btn {
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 6px;
  color: #909399;
  border: 1px solid transparent;
  transition: all 0.15s;
  user-select: none;
}
.fb-btn:hover {
  background: #f2f3f5;
  color: #606266;
}
.fb-btn.active {
  background: #ecf5ff;
  border-color: #b3d8ff;
  color: #409eff;
}
.chat-sources .el-collapse {
  border: none;
}
.chat-typing {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  padding: 14px 16px;
}
.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #c0c4cc;
  animation: typing-blink 1.2s infinite;
}
.typing-dot:nth-child(2) {
  animation-delay: 0.2s;
}
.typing-dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes typing-blink {
  0%, 80%, 100% { opacity: 0.3; }
  40% { opacity: 1; }
}
.chat-input-bar {
  border-top: 1px solid #e4e7ed;
  padding: 14px 20px;
  display: flex;
  gap: 10px;
  background: #fff;
  flex-shrink: 0;
}
.chat-input-bar .el-input {
  flex: 1;
}
.chat-input-bar .el-button {
  padding: 0 24px;
  border-radius: 6px;
}
</style>
