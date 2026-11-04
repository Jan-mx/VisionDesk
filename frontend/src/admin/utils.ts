export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

export function formatDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return '从未登录'
  try {
    const d = new Date(dateStr)
    if (isNaN(d.getTime())) return dateStr
    return d.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return dateStr
  }
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${ms}ms`
  const sec = (ms / 1000).toFixed(1)
  if (ms < 60000) return `${sec}s`
  const min = Math.floor(ms / 60000)
  const remainingSec = Math.floor((ms % 60000) / 1000)
  return `${min}m ${remainingSec}s`
}

/** 工具注册表 23 项的中文标签（与后端 app.tools.label_of 同源，2026-10-26 对齐） */
export const TOOL_LABELS: Record<string, string> = {
  generate_image: '生成图片',
  replace_background: '换背景',
  expand_canvas: '扩图',
  upscale_image: '超分',
  remove_background: '去背景',
  adjust_image: '调色',
  erase_region: '局部消除',
  replace_region: '局部替换',
  split_layers: '拆层',
  promote_object_to_layer: '提升为图层',
  generate_marketing: '营销图',
  prepare_delivery_sizes: '投放尺寸',
  batch_process: '批量处理',
  create_color_variants: '多颜色主体',
  crop_canvas: '裁剪',
  flip_layer: '翻转',
  set_layer_opacity: '透明度',
  set_layer_visible: '显隐',
  set_layer_text: '改文字',
  reorder_layer: '图层顺序',
  scale_layer: '缩放',
  rotate_layer: '旋转',
  move_layer: '移动',
}

export function toolDisplayName(tool: string | null | undefined): string {
  if (!tool) return ''
  return TOOL_LABELS[tool] ?? tool
}

export const TOOL_OPTIONS = Object.entries(TOOL_LABELS).map(([id, name]) => ({ id, name }))

/** 素材 kind 中文标签（与后端 AssetKind 枚举一一对应） */
export const KIND_LABELS: Record<string, string> = {
  original: '用户原图',
  generated: 'AI 生成',
  subject: '主体抠图',
  background: '背景元素',
  mask: '涂抹遮罩',
  snapshot: '快照存档',
  marketing: '营销图',
  export: '导出产物',
}

export function kindDisplayName(kind: string | null | undefined): string {
  if (!kind) return ''
  return KIND_LABELS[kind] ?? kind
}
