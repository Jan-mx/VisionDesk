import type { Asset, AssetKind } from '@/api/assets'
import { useEditorUi } from '@/stores/editorUi'

const KIND_LABELS: Record<AssetKind, string> = {
  original: '原图',
  generated: '生成',
  subject: '主体',
  background: '背景',
  mask: '遮罩',
  marketing: '营销',
  export: '导出',
  snapshot: '画布',
}

/** selector 返回的空集合必须引用稳定，否则 zustand v5 会无限重渲染 */
const NO_SELECTION: string[] = []

/** 会话内全部图片胶卷墙（复现 creative-v1 方案 A · 104px 高度独立滚动） */
export default function ImageWall({
  assets,
  currentId,
  sessionId,
  disabled,
  onPick,
}: {
  assets: Asset[]
  currentId: string
  sessionId: string
  disabled: boolean
  onPick: (assetId: string) => void
}) {
  // 最新一张快照常驻墙上：点走其他素材后，最新画布版本仍有入口可点回
  const latestSnapshotId = [...assets].reverse().find((asset) => asset.kind === 'snapshot')?.id
  const visibleAssets = assets
    .filter((asset) => asset.kind !== 'mask')
    .filter(
      (asset) =>
        asset.id === currentId ||
        asset.id === latestSnapshotId ||
        (asset.kind !== 'subject' && asset.kind !== 'background' && asset.kind !== 'snapshot'),
    )

  const selectMode = useEditorUi((s) => s.wallSelectMode)
  const selectedIds = useEditorUi((s) => s.wallSelectedIds)
  const toggleSelect = useEditorUi((s) => s.toggleWallSelect)
  const setSelectMode = useEditorUi((s) => s.setWallSelectMode)
  const clearSelection = useEditorUi((s) => s.clearWallSelection)
  // 勾选与多选模式都携带会话戳：切走会话后 store 里的旧选择不再生效（E2E-F02）
  const wallSelectionFor = useEditorUi((s) => s.wallSelectionFor)
  const scopedSelectedIds = wallSelectionFor === sessionId ? selectedIds : NO_SELECTION
  const selectableIds = new Set(
    visibleAssets
      .filter((asset) => ['original', 'generated', 'marketing', 'export'].includes(asset.kind))
      .map((asset) => asset.id),
  )
  const selectedCount = scopedSelectedIds.filter((id) => selectableIds.has(id)).length

  return (
    <div className="border-line bg-paper flex h-[104px] shrink-0 items-center gap-3 border-t px-5">
      <div className="flex shrink-0 flex-col text-left">
        <div className="flex items-center gap-2">
          <span className="text-ink text-xs font-bold">图片墙</span>
          {selectMode ? (
            <button
              type="button"
              onClick={() => clearSelection()}
              className="text-brand text-[10.5px] font-semibold hover:underline"
            >
              完成{selectedCount > 0 ? `（${selectedCount}）` : ''}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setSelectMode(true, sessionId)}
              className="text-muted hover:text-brand text-[10.5px] font-medium hover:underline"
              title="勾选多张素材后，在 AI 对话里说「这些图都…」即可批量处理"
            >
              选择
            </button>
          )}
        </div>
        <span className="text-muted text-[10.5px] whitespace-nowrap">
          {selectMode
            ? selectedCount > 0
              ? `已选 ${selectedCount} 张，去对话框说「这些图都…」`
              : '点击素材加入选择'
            : `${visibleAssets.length} 张素材`}
        </span>
      </div>

      <div className="scrollbar-slim flex min-w-0 flex-1 items-center gap-2.5 overflow-x-auto py-2">
        {visibleAssets.map((asset) => {
          const active = asset.id === currentId
          const checked = scopedSelectedIds.includes(asset.id)
          const selectable = selectableIds.has(asset.id)
          return (
            <button
              key={asset.id}
              type="button"
              disabled={disabled || (!selectMode && active)}
              onClick={() =>
                selectMode && selectable ? toggleSelect(asset.id, sessionId) : onPick(asset.id)
              }
              title={
                selectMode
                  ? selectable
                    ? checked
                      ? '取消选择'
                      : '加入选择'
                    : '该素材不支持批量处理'
                  : active
                    ? `当前画布 · ${KIND_LABELS[asset.kind]}`
                    : `采用这张 · ${KIND_LABELS[asset.kind]} ${asset.width}×${asset.height}`
              }
              className={`bg-canvas group rounded-[8px] relative flex h-[72px] w-[64px] shrink-0 flex-col items-center justify-center overflow-hidden border-2 transition-all duration-150 disabled:cursor-default ${
                selectMode && checked
                  ? 'border-brand shadow-xs'
                  : selectMode && !selectable
                    ? 'border-line opacity-40'
                    : active
                      ? 'border-brand shadow-xs'
                      : 'border-line hover:border-brand/70 hover:shadow-xs active:scale-[0.97]'
              }`}
            >
              <img
                src={asset.url}
                alt=""
                loading="lazy"
                className="h-[52px] w-full object-contain transition-transform duration-150 group-hover:scale-[1.03]"
              />
              <span className="text-muted mt-0.5 block truncate px-0.5 text-center text-[10px]">
                {selectMode
                  ? checked
                    ? '已选'
                    : KIND_LABELS[asset.kind]
                  : active
                    ? '当前作品'
                    : KIND_LABELS[asset.kind]}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
