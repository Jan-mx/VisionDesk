import type { Asset, AssetKind } from '@/api/assets'

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

/** 会话内全部图片胶卷墙（复现 creative-v1 方案 A · 104px 高度独立滚动） */
export default function ImageWall({
  assets,
  currentId,
  disabled,
  onPick,
}: {
  assets: Asset[]
  currentId: string
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

  return (
    <div className="border-line bg-paper flex h-[104px] shrink-0 items-center gap-3 border-t px-5">
      <div className="flex shrink-0 flex-col text-left">
        <span className="text-ink text-xs font-bold">图片墙</span>
        <span className="text-muted text-[10.5px] whitespace-nowrap">{visibleAssets.length} 张素材</span>
      </div>

      <div className="scrollbar-slim flex min-w-0 flex-1 items-center gap-2.5 overflow-x-auto py-2">
        {visibleAssets.map((asset) => {
          const active = asset.id === currentId
          return (
            <button
              key={asset.id}
              type="button"
              disabled={disabled || active}
              onClick={() => onPick(asset.id)}
              title={
                active
                  ? `当前画布 · ${KIND_LABELS[asset.kind]}`
                  : `采用这张 · ${KIND_LABELS[asset.kind]} ${asset.width}×${asset.height}`
              }
              className={`bg-canvas group rounded-[8px] relative flex h-[72px] w-[64px] shrink-0 flex-col items-center justify-center overflow-hidden border-2 transition-all duration-150 disabled:cursor-default ${
                active
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
                {active ? '当前作品' : KIND_LABELS[asset.kind]}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
