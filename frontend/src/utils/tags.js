/**
 * 展示层映射工具：把后端枚举值映射成 element-plus 的 tag 类型。
 * 抽到独立模块供各竞品组件复用，避免同一份映射在多个组件里各写一遍而漂移。
 */

export function typeTag(type) {
  const map = { 'C2C 闲置交易': 'primary', '回收置换': 'warning', '质检转卖': 'success' }
  return map[type] || 'info'
}

export function statusTag(status) {
  const map = { 已上线: 'success', 内测: 'warning', 已下线: 'info' }
  return map[status] || 'info'
}

export function dataStatusTag(status) {
  const map = { draft: 'info', 待校对: 'warning', 已入库: 'success' }
  return map[status] || 'info'
}

/** 平台类型 / 上线状态的可选项（录入表单与筛选共用） */
export const TYPE_OPTIONS = ['C2C 闲置交易', '回收置换', '质检转卖']
export const STATUS_OPTIONS = ['已上线', '内测', '已下线']
