# frontend/CLAUDE.md

Vue 3 + Vite + Element Plus 后台界面。**没有前端测试基建，也没有 TS** —— 改动的质量门
就是 `npm run build`（vite build 通过即算过），所以关键约定都写在这里。

## 共享状态：组合式函数 + 模块级单例（不引 Pinia）

- `src/stores/*.js` 里 `const xxx = ref(...)` 是**模块级单例**，`useXxxStore()` 只把
  ref 与 action 打包返回。
- ⚠️ 组件必须**顶层解构**才能让模板自动解包：
  `const { a, b } = useXxxStore()` ✅ / `const s = useXxxStore()` 后模板写 `s.a` ❌（渲染出 ref 对象）。
- 跨组件数据变更统一走 `notifyChanged()`（版本号），不要写回调链。

## SSE 消费：问答流走 `api.js::readSseData`

- `ragAskStream`（问答）经 `readSseData(resp)` 按 `\n\n` 切帧、取 `data:` 行；
  **问答流以 `[DONE]` 收尾**，加新的流式接口时先确认属于哪一种收尾形态。
- 流式请求失败统一走 `throwStreamError(resp)`（401 清登录态并广播 `auth-expired`）。
- 需要可中止就传 `AbortSignal`；**组件 `onBeforeUnmount` 必须中止**（不中止会留下
  悬挂连接）。

## 视觉规范（2026-09-20 视觉升级后）

- 设计令牌集中在 `src/styles/global.css`（石板灰中性色 + 靛蓝品牌色），**业务样式
  禁止直写 hex**，用 `--ink-*` / `--line-*` / `--surface` / `--brand-*` 语义别名。
- 图标一律用 `@element-plus/icons-vue`，**模板里不写 emoji**；主题色覆盖走
  Element Plus 的 `--el-color-*` 变量（light-3/5/7/8/9 + dark-2 全层级）。
- 跨页面共享类（`.card` / `.toolbar` / `.section` / `.rag-tip` 等）定义在
  global.css，组件内**不要再重定义**。

## Agent 模块（已撤回）

> Agent 相关页面/store/API 已于 commit `e3291d1` 随模块整体撤回；
> 历史约定见该提交之前的版本。
