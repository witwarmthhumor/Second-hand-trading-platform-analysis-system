/**
 * 通用展示格式化工具（跨组件复用，避免各面板各写一份）。
 */

/** ISO 时间 → 本地可读格式（YYYY/M/D HH:mm:ss，24 小时制）；空值显示 -。 */
export function fmtTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
