/**
 * 能力卡片的示意图：全部用样式拼出产品界面，不依赖任何图片资源。
 * 适配 creative-v1 方案 A 视觉系统（暖米白底、电光蓝、马卡龙辅色）。
 * 均为装饰性内容，对辅助技术隐藏。
 */

const TILES = [
  { bg: 'bg-gradient-to-br from-lavender/60 via-canvas to-paper', border: 'border-lavender-border' },
  { bg: 'bg-gradient-to-br from-sky/70 via-brand-soft to-paper', border: 'border-brand' },
  { bg: 'bg-gradient-to-br from-mint/60 via-canvas to-paper', border: 'border-mint-border' },
  { bg: 'bg-gradient-to-br from-peach/60 via-canvas to-paper', border: 'border-peach-border' },
]

/** 一句话生成：四个候选方向，其中一个被选中。 */
export function CandidatesPreview() {
  return (
    <div className="grid aspect-square h-full grid-cols-2 gap-2.5 p-1" aria-hidden>
      {TILES.map((tile, index) => {
        const isSelected = index === 1
        return (
          <div
            key={index}
            className={`relative grid place-items-center rounded-[10px] border shadow-xs transition-transform ${tile.bg} ${
              isSelected ? 'ring-2 ring-brand border-brand shadow-sm scale-[1.02]' : tile.border
            }`}
          >
            <span className="bg-ink/10 h-6 w-4.5 rounded-[4px] shadow-xs" />
            {isSelected && (
              <span className="bg-brand absolute top-1.5 right-1.5 grid size-4 place-items-center rounded-full shadow-xs">
                <svg viewBox="0 0 24 24" fill="none" className="size-2.5">
                  <path d="M5 13l4.5 4.5L19 7" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 主体级编辑：选区框住主体，指令只作用于框内。 */
export function SelectionPreview() {
  return (
    <div
      className="border-line relative aspect-[4/3] h-full max-h-full w-auto overflow-hidden rounded-[13px] border bg-gradient-to-br from-lavender/30 via-canvas to-mint/20"
      aria-hidden
    >
      <div className="border-brand bg-brand-soft/20 absolute top-[18%] left-[22%] h-[54%] w-[38%] rounded-[6px] border-2 border-dashed">
        <span className="bg-ink/12 absolute inset-1.5 rounded-[4px]" />
        {['-top-1 -left-1', '-top-1 -right-1', '-bottom-1 -left-1', '-bottom-1 -right-1'].map((pos) => (
          <span key={pos} className={`bg-paper border-brand absolute size-2 rounded-full border-2 shadow-xs ${pos}`} />
        ))}
      </div>
      <p className="bg-ink/85 backdrop-blur-md absolute bottom-2.5 left-2.5 flex items-center gap-1.5 rounded-full px-3 py-1 text-[10.5px] font-medium text-white shadow-md">
        <span className="text-accent text-[11px]">✦</span>
        <span>把背景换成米色亚麻</span>
      </p>
    </div>
  )
}

const LAYERS = [
  { name: '文字 · 卖点标注', swatch: 'bg-peach border border-peach-border', indent: 'ml-0' },
  { name: '主体 · 商品', swatch: 'bg-brand text-white', indent: 'ml-3' },
  { name: '背景 · 场景', swatch: 'bg-mint border border-mint-border', indent: 'ml-6' },
]

/** 语义图层：主体、背景、文字自动分层。 */
export function LayersPreview() {
  return (
    <div className="w-full max-w-[210px] space-y-1.5" aria-hidden>
      {LAYERS.map((layer) => (
        <div
          key={layer.name}
          className={`border-line bg-paper shadow-control hover:border-brand/40 flex items-center gap-2.5 rounded-[10px] border px-3 py-2 transition-colors ${layer.indent}`}
        >
          <span className={`size-4 shrink-0 rounded-[4px] ${layer.swatch}`} />
          <span className="text-ink flex-1 truncate text-xs font-medium">{layer.name}</span>
          <svg viewBox="0 0 24 24" fill="none" className="text-muted-light size-3.5">
            <path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z" stroke="currentColor" strokeWidth="1.8" />
            <circle cx="12" cy="12" r="2.5" stroke="currentColor" strokeWidth="1.8" />
          </svg>
        </div>
      ))}
    </div>
  )
}

const RATIOS = [
  { label: '1:1', frame: 'h-[64px] w-[64px]' },
  { label: '4:5', frame: 'h-[72px] w-[58px]' },
  { label: '9:16', frame: 'h-[80px] w-[46px]' },
]

/** 物料包交付：一次导出多个投放尺寸。 */
export function ExportsPreview() {
  return (
    <div className="flex items-end gap-3" aria-hidden>
      {RATIOS.map((ratio) => (
        <div key={ratio.label} className="flex flex-col items-center gap-1.5">
          <div
            className={`border-line bg-canvas/90 grid place-items-center rounded-[9px] border shadow-xs ${ratio.frame}`}
          >
            <span className="bg-ink/12 h-2/5 w-1/3 rounded-[3px]" />
          </div>
          <span className="text-muted text-[10.5px] font-semibold">{ratio.label}</span>
        </div>
      ))}
    </div>
  )
}
