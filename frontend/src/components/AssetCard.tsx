import type { Asset } from '@/api/assets'
import { formatBytes, formatDateTime } from '@/lib/format'

export interface AssetCardProps {
  asset: Asset
  onSelect?: () => void
  onRename?: () => void
  onDelete?: () => void
  selectable?: boolean
  selected?: boolean
  onToggleSelect?: () => void
}

export default function AssetCard({
  asset,
  onSelect,
  onRename,
  onDelete,
  selectable = false,
  selected = false,
  onToggleSelect,
}: AssetCardProps) {
  const hasActions = Boolean(onRename || onDelete || onSelect)

  const handleCardClick = () => {
    if (selectable && onToggleSelect) {
      onToggleSelect()
    } else if (onSelect) {
      onSelect()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      if (e.target === e.currentTarget) {
        e.preventDefault()
        handleCardClick()
      }
    }
  }

  return (
    <div
      role="article"
      tabIndex={0}
      onClick={handleCardClick}
      onKeyDown={handleKeyDown}
      title={asset.title || '点击查看/编辑'}
      className={`border-line bg-paper group relative flex flex-col overflow-hidden rounded-[18px] border text-left transition-all duration-150 outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-1 ${
        selected
          ? 'border-brand ring-2 ring-brand/20'
          : 'hover:border-brand-strong/60 hover:shadow-xs'
      }`}
    >
      {/* 缩略图区域 */}
      <div className="bg-canvas relative aspect-square w-full overflow-hidden select-none">
        <img
          src={asset.url}
          alt={asset.title || '素材'}
          loading="lazy"
          className="size-full object-contain transition-transform duration-200 group-hover:scale-[1.02]"
        />

        {/* 透明底标签 */}
        {asset.has_alpha && (
          <span className="bg-ink/80 absolute bottom-2 left-2 rounded px-1.5 py-0.5 text-[10px] font-medium text-white shadow-xs">
            透明底
          </span>
        )}

        {/* 多选勾选框 */}
        {selectable && (
          <div
            className="absolute top-1.5 left-1.5 z-10 flex min-h-[44px] min-w-[44px] items-center justify-center"
            onClick={(e) => {
              e.stopPropagation()
              // 命中 input 时由其 onChange 单次切换；此处仅兜底热区空白，避免双重 toggle 抵消
              if (e.target === e.currentTarget) onToggleSelect?.()
            }}
          >
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelect?.()}
              className="accent-brand size-4 cursor-pointer rounded border-line"
              aria-label={`选择素材 ${asset.title || asset.id}`}
            />
          </div>
        )}

        {/* 悬浮/触屏操作栏：触屏或无 hover 设备常显，保证 ≥44×44 点击热区 */}
        {hasActions && !selectable && (
          <div
            className="absolute top-1.5 right-1.5 z-10 flex items-center gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:opacity-100"
            onClick={(e) => e.stopPropagation()}
          >
            {onRename && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onRename()
                }}
                className="bg-paper/95 text-muted hover:text-ink hover:bg-paper border-line shadow-xs flex min-h-[44px] min-w-[44px] items-center justify-center rounded-control border transition-transform active:scale-95"
                title="重命名素材"
                aria-label="重命名素材"
              >
                <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                  <path
                    d="m14 5 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            )}

            {onDelete && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onDelete()
                }}
                className="bg-paper/95 text-muted hover:text-danger hover:bg-peach border-line shadow-xs flex min-h-[44px] min-w-[44px] items-center justify-center rounded-control border transition-transform active:scale-95"
                title="删除素材"
                aria-label="删除素材"
              >
                <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                  <path
                    d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            )}
          </div>
        )}
      </div>

      {/* 信息区：title 有则显示，无则回退元信息 */}
      <div className="flex flex-1 flex-col justify-between p-3 text-xs">
        {asset.title ? (
          <div>
            <div className="text-ink truncate text-xs font-semibold" title={asset.title}>
              {asset.title}
            </div>
            <div className="text-muted mt-0.5 text-[11px] tabular-nums">
              {asset.width} × {asset.height} · {asset.image_format} · {formatBytes(asset.size_bytes)}
            </div>
            <div className="text-faint mt-0.5 text-[10px] tabular-nums">
              {formatDateTime(asset.created_at)}
            </div>
          </div>
        ) : (
          <dl className="text-muted space-y-0.5 text-xs">
            <div className="text-ink font-medium tabular-nums">
              {asset.width} × {asset.height}
            </div>
            <div className="tabular-nums">
              {asset.image_format} · {formatBytes(asset.size_bytes)}
            </div>
            <div className="text-faint text-[10px] tabular-nums">
              {formatDateTime(asset.created_at)}
            </div>
          </dl>
        )}
      </div>
    </div>
  )
}
