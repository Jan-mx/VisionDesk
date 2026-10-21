import { useState } from 'react'
import { Link } from 'react-router-dom'

import type { SessionDetail } from '@/api/sessions'
import Button from '@/components/ui/Button'
import type { SessionSelection } from '@/hooks/useSelection'
import type { SessionTools } from '@/hooks/useSessions'
import { wallOnly } from '@/lib/layers'
import { useEditorUi, type CropRatio } from '@/stores/editorUi'

const CROP_RATIOS: { value: CropRatio; label: string }[] = [
  { value: 'free', label: '自由' },
  { value: '1:1', label: '1:1' },
  { value: '4:5', label: '4:5' },
  { value: '9:16', label: '9:16' },
  { value: '16:9', label: '16:9' },
]

/**
 * 编辑器专用顶栏与工具条（严格复现 creative-v1 方案 A · Editor 规范）
 * - 顶栏：文档标题、尺寸说明、演示标签、撤销/重做、导出物料主按钮
 * - 工具条：常用工具行（裁剪、去背、换背、扩图、调色、局部、更多、图层）
 * - 展开抽屉：翻转、超分、拆层、对比
 * - 在 ≥1280px 视口下零横向滚动条
 */
export default function EditorToolbar({
  session,
  tools,
  picking,
  onRename,
}: {
  session: SessionDetail
  tools: SessionTools
  picking: SessionSelection
  onRename: (title: string) => void
}) {
  const [moreToolsOpen, setMoreToolsOpen] = useState(false)
  const ui = useEditorUi()

  const running = (tool: string, match?: (params: Record<string, unknown>) => boolean) =>
    tools.isRunning(tool, match) ? tools.pendingProgress : null

  const confirmCrop = () => {
    if (!ui.cropRect) return
    tools.invoke('crop_canvas', {
      rect: {
        x: ui.cropRect.x / session.document.width,
        y: ui.cropRect.y / session.document.height,
        width: ui.cropRect.width / session.document.width,
        height: ui.cropRect.height / session.document.height,
      },
    })
    ui.closeCrop()
  }

  return (
    <div className="flex flex-col shrink-0">
      {/* 1. 独立文档顶栏 (Editor Header, 58px) */}
      <div className="border-line bg-paper flex h-[58px] items-center justify-between border-b px-5">
        <div className="flex items-center gap-3">
          <Link
            to="/create"
            className="text-muted hover:text-ink hover:bg-soft grid size-8 place-items-center rounded-control transition-colors"
            title="返回创作页"
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
              <path d="M19 12H5m6-6-6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </Link>

          <div>
            <TitleField value={session.title} onCommit={onRename} />
            <div className="text-muted flex items-center gap-2 px-2 text-[11px]">
              <span className="font-mono">{session.document.width} × {session.document.height}</span>
              <span>·</span>
              <span>会话编辑中</span>
            </div>
          </div>

          <span className="bg-mint text-[#2b6a48] border-mint-border hidden sm:inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold">
            <span className="size-1.5 rounded-full bg-current" />
            当前会话
          </span>
        </div>

        <div className="flex items-center gap-2">
          {/* 撤销与重做 */}
          <Button
            disabled={tools.busy || picking.busy || !(picking.canUndo || session.can_undo)}
            title={
              picking.canUndo
                ? '撤销选区 ⌘Z'
                : session.can_undo
                  ? '撤销上一步 ⌘Z'
                  : '没有可撤销的操作'
            }
            onClick={() => (picking.canUndo ? picking.undo() : tools.undo())}
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
              <path d="M8 4 3 9l5 5M3 9h11a6 6 0 0 1 0 12" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="hidden sm:inline">撤销</span>
          </Button>

          <Button
            disabled={tools.busy || picking.busy || !(picking.canRedo || session.can_redo)}
            title={
              picking.canRedo
                ? '重做选区 ⌘⇧Z'
                : session.can_redo
                  ? '重做 ⌘⇧Z'
                  : '没有可重做的操作'
            }
            onClick={() => (picking.canRedo ? picking.redo() : tools.redo())}
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
              <path d="m16 4 5 5-5 5m5-5H10a6 6 0 0 0 0 12" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="hidden sm:inline">重做</span>
          </Button>

          {/* 导出物料行动按钮 */}
          <Link
            to={`/marketing/${session.id}`}
            title="导出营销图和投放尺寸"
            className="bg-brand hover:bg-brand-strong rounded-control shadow-xs flex items-center gap-1.5 px-3.5 py-2 text-xs font-semibold text-white transition-all hover:shadow-md"
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
              <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>导出物料</span>
          </Link>
        </div>
      </div>

      {/* 2. 常用编辑工具条 (Editor Tools Row, 46px) */}
      <div className="border-line bg-[#f9f9fc] flex min-h-[46px] items-center gap-1.5 border-b px-4 py-1.5 flex-wrap">
        {ui.selectMode ? (
          /* 局部选区模式 */
          <>
            <span className="text-brand text-xs font-bold mr-1">局部选区：</span>
            <Button
              active={ui.selectMode === 'point'}
              title="点击物体生成选区"
              onClick={() => ui.setSelectMode(ui.selectMode === 'point' ? null : 'point')}
            >
              点选
            </Button>
            <Button
              active={ui.selectMode === 'brush'}
              title="涂抹出选区"
              onClick={() => ui.setSelectMode(ui.selectMode === 'brush' ? null : 'brush')}
            >
              笔刷
            </Button>
            <Button
              disabled={tools.busy || picking.busy || !picking.selection}
              progress={running('erase_region')}
              title={picking.selection ? '消除选中区域' : '先点选或涂抹'}
              onClick={() =>
                tools.invoke('erase_region', {
                  mask_asset_id: picking.selection?.maskId,
                  revision: session.revision,
                })
              }
            >
              消除
            </Button>
            <Button
              disabled={!picking.selection}
              active={ui.panel === 'replace'}
              title={picking.selection ? '按描述替换选区' : '先点选或涂抹'}
              onClick={() => ui.setPanel(ui.panel === 'replace' ? null : 'replace')}
            >
              替换
            </Button>
            <Button
              disabled={tools.busy || picking.busy || !picking.selection}
              progress={running('promote_object_to_layer')}
              title={picking.selection ? '把选区提升为独立图层，可撤销' : '先点选或涂抹'}
              onClick={() =>
                tools.invoke('promote_object_to_layer', {
                  mask_asset_id: picking.selection?.maskId,
                  revision: session.revision,
                })
              }
            >
              成层
            </Button>
            <Button
              disabled={!picking.selection || picking.busy}
              title="清除当前选区"
              onClick={picking.clear}
            >
              清除
            </Button>
            <Button title="退出选择 Esc" onClick={() => ui.setSelectMode(null)} className="ml-auto">
              完成
            </Button>
          </>
        ) : ui.cropOpen ? (
          /* 裁剪模式 */
          <>
            <span className="text-brand text-xs font-bold mr-1">裁剪比例：</span>
            {CROP_RATIOS.map((item) => (
              <Button
                key={item.value}
                active={ui.cropRatio === item.value}
                title={item.value === 'free' ? '自由裁剪' : `按 ${item.label} 裁剪`}
                onClick={() => ui.setCropRatio(item.value, session.document)}
              >
                {item.label}
              </Button>
            ))}
            <Button title="按当前框裁剪画布，可撤销" onClick={confirmCrop} className="ml-auto">
              确定
            </Button>
            <Button title="退出裁剪 Esc" onClick={ui.closeCrop}>
              取消
            </Button>
          </>
        ) : (
          /* 原型常用工具行：裁剪 / 去背景 / 换背景 / 扩图 / 调色 | 局部 / 更多 | 图层 */
          <>
            <Button
              disabled={tools.busy}
              active={ui.cropOpen}
              title="按比例裁剪画布"
              onClick={() => ui.openCrop(session.document)}
            >
              裁剪
            </Button>

            <Button
              disabled={tools.busy}
              progress={running('remove_background')}
              title="抠出主体，背景变透明"
              onClick={() => tools.invoke('remove_background', { layer_id: ui.selectedLayerId })}
            >
              去背景
            </Button>

            <Button
              disabled={tools.busy}
              active={ui.panel === 'background'}
              title="按描述替换背景"
              onClick={() => ui.setPanel(ui.panel === 'background' ? null : 'background')}
            >
              换背景
            </Button>

            <Button
              disabled={tools.busy}
              active={ui.panel === 'expand'}
              title="扩展画布到新比例"
              onClick={() => ui.setPanel(ui.panel === 'expand' ? null : 'expand')}
            >
              扩图
            </Button>

            <Button
              active={ui.panel === 'adjust'}
              title="调整亮度、对比度和色彩"
              onClick={() => ui.setPanel(ui.panel === 'adjust' ? null : 'adjust')}
            >
              调色
            </Button>

            <div className="bg-line h-4 w-px mx-1" aria-hidden />

            <Button
              disabled={tools.busy}
              active={Boolean(ui.selectMode)}
              title="局部精修选区工具"
              onClick={() => ui.setSelectMode('point')}
            >
              局部
            </Button>

            <Button
              active={moreToolsOpen}
              title="更多扩展工具（翻转、超分、拆层、对比）"
              onClick={() => setMoreToolsOpen((prev) => !prev)}
            >
              更多 ▾
            </Button>

            <div className="bg-line h-4 w-px mx-1" aria-hidden />

            <Button
              active={ui.panel === 'layers'}
              title="图层管理与属性面板"
              onClick={() => ui.setPanel(ui.panel === 'layers' ? null : 'layers')}
            >
              图层
            </Button>
          </>
        )}
      </div>

      {/* 3. 展开抽屉栏 (Extra Tools Drawer, 当点击更多时展开) */}
      {moreToolsOpen && !ui.selectMode && !ui.cropOpen && (
        <div className="border-line bg-[#f4f3fb] flex items-center gap-2 border-b px-4 py-1.5 animate-slide-in">
          <span className="text-muted text-[11px] font-semibold mr-1">扩展工具：</span>
          <Button
            disabled={tools.busy}
            progress={running('flip_layer', (params) => params.direction === 'horizontal')}
            title="左右翻转当前图层"
            onClick={() =>
              tools.invoke('flip_layer', {
                direction: 'horizontal',
                layer_id: ui.selectedLayerId,
              })
            }
          >
            水平翻转
          </Button>

          <Button
            disabled={tools.busy}
            progress={running('flip_layer', (params) => params.direction === 'vertical')}
            title="上下翻转当前图层"
            onClick={() =>
              tools.invoke('flip_layer', {
                direction: 'vertical',
                layer_id: ui.selectedLayerId,
              })
            }
          >
            垂直翻转
          </Button>

          {([2, 4] as const).map((scale) => (
            <Button
              key={scale}
              disabled={tools.busy}
              progress={running('upscale_image', (params) => params.scale === scale)}
              title={
                wallOnly(session.document)
                  ? `放大到 ${scale} 倍，已拆层所以结果只进图片墙`
                  : `把当前画布放大到 ${scale} 倍`
              }
              onClick={() => tools.invoke('upscale_image', { scale })}
            >
              超分 {scale}X
            </Button>
          ))}

          <Button
            disabled={tools.busy}
            progress={running('split_layers')}
            title="拆成背景和主体，文字需在图层面板勾选，可撤销"
            onClick={() => tools.invoke('split_layers', { include_text: ui.splitIncludeText })}
          >
            拆层
          </Button>

          <Button
            active={ui.compareOpen}
            disabled={!session.previous_document}
            title={session.previous_document ? '与上一版对比' : '还没有上一版'}
            onClick={() => ui.setCompareOpen(!ui.compareOpen)}
          >
            对比
          </Button>

          <button
            type="button"
            onClick={() => setMoreToolsOpen(false)}
            className="text-muted hover:text-ink ml-auto text-xs px-2 py-1"
            title="收起扩展工具"
          >
            ✕ 收起
          </button>
        </div>
      )}
    </div>
  )
}

function TitleField({ value, onCommit }: { value: string; onCommit: (title: string) => void }) {
  const [draft, setDraft] = useState<string | null>(null)

  const commit = () => {
    const next = draft?.trim()
    setDraft(null)
    if (next && next !== value) onCommit(next)
  }

  return (
    <input
      value={draft ?? value}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur()
        if (event.key === 'Escape') setDraft(null)
      }}
      aria-label="会话标题"
      className="text-ink hover:bg-soft focus:bg-soft focus:ring-2 focus:ring-brand/20 rounded-chip w-36 shrink-0 px-2 py-1 text-sm font-semibold transition-all outline-none sm:w-48"
    />
  )
}
