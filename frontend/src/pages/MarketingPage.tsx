import { useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import AssetLibrary from '@/components/AssetLibrary'
import ProgressBar from '@/components/ui/ProgressBar'
import { errorMessage } from '@/hooks/useAuth'
import { useAssetLibrary } from '@/hooks/useAssets'
import { useCreateSession, useExportPack, useSession, useSessionTools, useSessions } from '@/hooks/useSessions'
import { shortTitle } from '@/lib/format'
import {
  DELIVERY_RATIOS,
  KIND_LABELS,
  MARKETING_KINDS,
  defaultSelected,
  packable,
  type MarketingKind,
} from '@/lib/marketing'
import { toast } from '@/stores/toasts'

const KIND_ICONS: Record<MarketingKind, string> = {
  product: 'M5 7h14l2 14H3L5 7ZM8 8V5a4 4 0 0 1 8 0v3',
  scene: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2',
  model: 'M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2',
  poster: 'M5 3h14v18H5zM8 7h8M8 11h5m-5 4h8m-8 3h5',
}

const RATIO_CHANNEL_HINTS: Record<string, string> = {
  '1:1': '天猫 / 京东主图',
  '4:5': '小红书 / 淘宝详情',
  '9:16': '抖音全屏竖版',
}

export default function MarketingPage() {
  const { sessionId = '' } = useParams()
  return sessionId ? <Workspace sessionId={sessionId} /> : <Picker />
}

function Picker() {
  const navigate = useNavigate()
  const { data: groups = [], isPending } = useAssetLibrary()
  const createSession = useCreateSession()

  const open = (id: string) => navigate(`/marketing/${id}`)

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-8 sm:px-10">
      <PageTitle />
      <h2 className="text-ink mb-3 text-sm font-bold">选择会话</h2>
      {isPending ? (
        <p className="text-faint text-xs">加载会话中…</p>
      ) : groups.length === 0 ? (
        <p className="text-muted text-xs">
          还没有可导出的会话，先到
          <Link to="/create" className="text-brand font-semibold mx-1">
            创作
          </Link>
          打开一张图。
        </p>
      ) : (
        <AssetLibrary
          groups={groups}
          onOpenSession={open}
          onOpenAsset={(assetId) =>
            createSession.mutate(
              { current_asset_id: assetId },
              { onSuccess: (session) => open(session.id) },
            )
          }
        />
      )}
    </div>
  )
}

function Workspace({ sessionId }: { sessionId: string }) {
  const { data: session, isError } = useSession(sessionId)
  const tools = useSessionTools(sessionId)
  const pack = useExportPack(sessionId)

  // 1. 进入会话：类型草稿默认 poster，待生成尺寸草稿默认 1:1/4:5/9:16（严格对齐 §8.2）
  const [selectedKind, setSelectedKind] = useState<MarketingKind>('poster')
  const [copy, setCopy] = useState('')
  const [draftRatios, setDraftRatios] = useState<string[]>(['1:1', '4:5', '9:16'])
  const [picked, setPicked] = useState<string[] | null>(null)
  const [previewAssetId, setPreviewAssetId] = useState<string | null>(null)

  const selected = useMemo(() => {
    if (!session) return []
    return picked ?? defaultSelected(session.assets, session.current_asset_id)
  }, [session, picked])

  if (!session) {
    return (
      <div className="flex h-full min-h-[460px] flex-col items-center justify-center px-8 text-center">
        <h1 className="text-ink text-lg font-bold">{isError ? '会话不存在' : '加载中'}</h1>
        <p className="text-muted mt-1 text-xs">
          {isError ? '回到列表另选一个会话。' : '正在读取画布与素材'}
        </p>
        {isError && (
          <Link to="/marketing" className="text-brand mt-4 text-xs font-semibold">
            返回导出物料
          </Link>
        )}
      </div>
    )
  }

  const current = session.assets.find((asset) => asset.id === session.current_asset_id)
  const items = session.assets.filter((asset) => packable(asset, session.current_asset_id))
  const activePreviewAsset =
    session.assets.find((asset) => asset.id === previewAssetId) ?? current

  const toggle = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setPicked([...next])
  }

  const toggleAll = () => {
    const ids = items.map((asset) => asset.id)
    setPicked(ids.every((id) => selected.includes(id)) ? [] : ids)
  }

  // 勾选/取消勾选待生成尺寸（纯本地草稿，不改变打包选择与服务端任务，严格对齐 §8.2）
  const toggleDraftRatio = (ratio: string) => {
    setDraftRatios((prev) =>
      prev.includes(ratio) ? prev.filter((r) => r !== ratio) : [...prev, ratio],
    )
  }

  // 点击明确生成所选尺寸（严格对齐 §8.2）
  const generateSizes = () => {
    if (draftRatios.length === 0 || tools.busy) return
    if (draftRatios.length === DELIVERY_RATIOS.length) {
      tools.invoke('prepare_delivery_sizes')
    } else {
      tools.invoke('prepare_delivery_sizes', { ratios: draftRatios })
    }
  }

  // 点击明确生成营销物料（严格对齐 §8.2）
  const generateMarketing = () => {
    if (tools.busy) return
    tools.invoke('generate_marketing', {
      kind: selectedKind,
      caption: copy.trim() || undefined,
    })
  }

  const download = () =>
    pack.mutate(selected, {
      onSuccess: ({ blob, filename }) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        link.click()
        URL.revokeObjectURL(url)
        toast(`已打包 ${selected.length} 张物料`)
      },
    })

  return (
    <div className="mx-auto max-w-[1220px] px-6 py-8 sm:px-10">
      <PageTitle
        action={
          <Link
            to={`/editor/${sessionId}`}
            className="border-line bg-paper text-ink hover:bg-soft hover:border-line-strong rounded-control shadow-control flex items-center gap-1.5 border px-3.5 py-1.5 text-xs font-semibold"
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
              <path d="M19 12H5m6-6-6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>返回编辑器</span>
          </Link>
        }
      />

      {/* 复现 1:1 等宽双栏布局 (A·中央创作台规范) */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 items-start">
        {/* 左栏：物料生成配置面板 */}
        <div className="border-line bg-paper rounded-card shadow-card border p-6 flex flex-col gap-6">
          {/* 步骤 01：从这张图开始 */}
          <div>
            <div className="flex items-center gap-2.5 mb-3">
              <span className="bg-lavender text-brand font-bold text-xs size-6 rounded-md grid place-items-center">
                01
              </span>
              <h2 className="text-ink text-sm font-bold">从这张图开始</h2>
            </div>

            <div className="border-line bg-soft rounded-control flex items-center gap-3.5 border p-3">
              <div className="border-line size-16 shrink-0 overflow-hidden rounded-md border bg-white">
                {current && <img src={current.url} alt="" className="size-full object-contain" />}
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-ink truncate text-xs font-semibold" title={session.title}>
                  {shortTitle(session.title, 26)}
                </p>
                <p className="text-muted mt-1 text-[11px] font-mono">
                  画布 {session.document.width} × {session.document.height}
                </p>
              </div>
              <SessionSwitch currentId={sessionId} />
            </div>
          </div>

          <div className="border-line border-t" />

          {/* 步骤 02：你想创作什么？(2x2 类型选择) */}
          <div>
            <div className="flex items-center gap-2.5 mb-3">
              <span className="bg-lavender text-brand font-bold text-xs size-6 rounded-md grid place-items-center">
                02
              </span>
              <h2 className="text-ink text-sm font-bold">你想创作什么？</h2>
            </div>

            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
              {MARKETING_KINDS.map((item) => {
                const active = selectedKind === item.id
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedKind(item.id)}
                    className={`rounded-control border p-3 text-left transition-all ${
                      active
                        ? 'border-brand bg-brand-soft ring-2 ring-brand/10'
                        : 'border-line bg-paper hover:border-line-strong'
                    }`}
                  >
                    <div className="flex items-center gap-2.5">
                      <svg viewBox="0 0 24 24" fill="none" className={`size-4 ${active ? 'text-brand' : 'text-muted'}`} aria-hidden>
                        <path d={KIND_ICONS[item.id]} stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      <strong className="text-ink text-xs font-semibold">{item.label}</strong>
                    </div>
                    <span className="text-muted mt-1 block text-[11px] leading-tight">{item.hint}</span>
                  </button>
                )
              })}
            </div>

            <div className="mt-3.5">
              <label className="text-muted block text-[11.5px] font-medium mb-1">
                补充一点想法 <span className="text-faint">/ 可选</span>
              </label>
              <textarea
                value={copy}
                onChange={(event) => setCopy(event.target.value)}
                disabled={tools.busy}
                rows={2}
                placeholder="例如：新品上市，春日植萃系列，画面清新自然"
                className="border-line bg-paper text-ink placeholder:text-muted-light focus:border-brand/70 rounded-control w-full border px-3 py-2 text-xs outline-none"
              />
            </div>

            <button
              type="button"
              disabled={tools.busy}
              onClick={generateMarketing}
              className="bg-brand hover:bg-brand-strong rounded-control shadow-sm mt-3 flex w-full items-center justify-center gap-2 py-2.5 text-xs font-semibold text-white transition-all disabled:opacity-40"
            >
              <span>{tools.isRunning('generate_marketing') ? '正在生成营销图…' : '生成营销物料'}</span>
              <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
                <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>

          <div className="border-line border-t" />

          {/* 步骤 03：适配你的每一个渠道 (三种尺寸选择) */}
          <div>
            <div className="flex items-center gap-2.5 mb-3">
              <span className="bg-lavender text-brand font-bold text-xs size-6 rounded-md grid place-items-center">
                03
              </span>
              <h2 className="text-ink text-sm font-bold">适配你的每一个渠道</h2>
            </div>

            <div className="grid grid-cols-3 gap-2">
              {DELIVERY_RATIOS.map((ratio) => {
                const checked = draftRatios.includes(ratio.value)
                return (
                  <button
                    key={ratio.value}
                    type="button"
                    onClick={() => toggleDraftRatio(ratio.value)}
                    className={`rounded-control border p-2.5 text-center transition-all ${
                      checked
                        ? 'border-brand bg-brand-soft ring-1 ring-brand/10'
                        : 'border-line bg-paper hover:border-line-strong'
                    }`}
                  >
                    <span className="border-line-strong mx-auto mb-1.5 block size-5 rounded-xs border-1.5" />
                    <strong className="text-ink block text-xs font-semibold">{ratio.value}</strong>
                    <span className="text-muted block text-[10px] mt-0.5">{RATIO_CHANNEL_HINTS[ratio.value] || ratio.size}</span>
                  </button>
                )
              })}
            </div>

            <button
              type="button"
              disabled={tools.busy || draftRatios.length === 0}
              onClick={generateSizes}
              className="border-line text-ink hover:bg-soft rounded-control shadow-xs mt-3 flex w-full items-center justify-center gap-2 border py-2 text-xs font-semibold transition-all disabled:opacity-40"
            >
              <span>
                {tools.isRunning('prepare_delivery_sizes')
                  ? '正在生成尺寸…'
                  : `生成所选尺寸 (${draftRatios.length} 种)`}
              </span>
            </button>
          </div>

          {tools.busy && (
            <div className="mt-1">
              <div className="flex items-center justify-between text-[11px] text-muted">
                <span>{tools.pendingStage || '正在处理中'}</span>
                <span>{tools.pending ? `${tools.pendingProgress}%` : ''}</span>
              </div>
              {tools.pending && (
                <ProgressBar value={tools.pendingProgress} tone="brand" className="mt-1.5 h-1 rounded-full" />
              )}
            </div>
          )}
        </div>

        {/* 右栏：成果交付展台与真实打包下载 */}
        <div className="flex flex-col gap-6">
          {/* 大图效果展台卡片 */}
          <section className="bg-[#f3f2ee] border-[#e3e2dc] rounded-card border p-5 shadow-sm text-center">
            <div className="flex items-center justify-between mb-3 text-xs">
              <strong className="text-ink font-semibold">效果预览</strong>
              <span className="bg-mint text-[#2b6a48] border-mint-border rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold">
                真实产出
              </span>
            </div>

            <div className="flex items-center justify-center min-h-[340px] max-h-[400px] overflow-hidden rounded bg-white p-2">
              {activePreviewAsset && (
                <img
                  src={activePreviewAsset.url}
                  alt="当前效果预览"
                  className="max-h-[380px] w-auto max-w-full object-contain rounded shadow-sm"
                />
              )}
            </div>

            <div className="mt-3 flex items-center justify-between text-xs text-muted">
              <span>{KIND_LABELS[activePreviewAsset?.kind ?? 'original']} / 版式展示</span>
              <span className="font-mono">{activePreviewAsset?.width} × {activePreviewAsset?.height} · PNG</span>
            </div>
          </section>

          {/* 三比例交付预览列表 */}
          <div className="border-line bg-paper rounded-card shadow-sm border p-4">
            <div className="text-muted text-[11px] font-semibold mb-2 text-left">多尺寸交付示意</div>
            <div className="flex items-end justify-center gap-3">
              {DELIVERY_RATIOS.map((ratio) => {
                const isActive = draftRatios.includes(ratio.value)
                return (
                  <div
                    key={ratio.value}
                    className={`text-center transition-all duration-200 ${
                      isActive ? 'opacity-100 scale-100' : 'opacity-25 scale-95'
                    }`}
                  >
                    <div className="border-line size-20 overflow-hidden rounded border bg-canvas p-1 flex items-center justify-center">
                      {current && (
                        <img src={current.url} alt="" className="size-full object-contain" />
                      )}
                    </div>
                    <span className="text-muted mt-1 block text-[10.5px] font-medium">{ratio.value}</span>
                  </div>
                )
              })}
            </div>
          </div>

          {/* 打包下载专区 (严格按照 §8.2 基于真实 selectedAssetIds) */}
          <section className="border-line bg-paper rounded-card shadow-card border p-5">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div>
                <h3 className="text-ink text-sm font-bold">打包下载</h3>
                <p className="text-muted text-xs mt-0.5">
                  已选择 <span className="text-brand font-bold">{selected.length}</span> / {items.length} 张图片 · 内含 manifest.json
                </p>
              </div>

              <div className="flex items-center gap-2">
                {items.length > 0 && (
                  <button
                    type="button"
                    onClick={toggleAll}
                    className="border-line text-muted hover:text-ink rounded-control border px-2.5 py-1 text-xs font-medium"
                  >
                    {items.every((a) => selected.includes(a.id)) ? '取消全选' : '全选'}
                  </button>
                )}
                <button
                  type="button"
                  disabled={pack.isPending || tools.busy || selected.length === 0}
                  onClick={download}
                  className="bg-brand hover:bg-brand-strong rounded-control shadow-sm flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold text-white transition-all disabled:opacity-40"
                >
                  <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
                    <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span>{pack.isPending ? '正在打包…' : `打包导出 (${selected.length} 张)`}</span>
                </button>
              </div>
            </div>

            {/* 可勾选真实资产画廊列表 */}
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-2.5 max-h-60 overflow-y-auto scrollbar-slim pt-1">
              {items.map((asset) => {
                const isChecked = selected.includes(asset.id)
                return (
                  <div
                    key={asset.id}
                    onClick={() => {
                      toggle(asset.id)
                      setPreviewAssetId(asset.id)
                    }}
                    className={`border-1.5 relative flex flex-col rounded-[10px] overflow-hidden p-1 cursor-pointer transition-all ${
                      isChecked
                        ? 'border-brand bg-brand-soft shadow-xs'
                        : 'border-line bg-paper opacity-70 hover:opacity-100'
                    }`}
                  >
                    <img src={asset.url} alt="" className="h-16 w-full object-contain rounded" />
                    <span
                      className={`absolute top-2 right-2 size-4.5 rounded grid place-items-center text-[10px] ${
                        isChecked ? 'bg-brand text-white' : 'bg-paper/80 border border-line text-transparent'
                      }`}
                    >
                      ✓
                    </span>
                    <span className="text-muted mt-1 block truncate text-center text-[10px]">
                      {KIND_LABELS[asset.kind]} {asset.width}×{asset.height}
                    </span>
                  </div>
                )
              })}
            </div>

            {pack.isError && (
              <p className="text-danger mt-3 text-xs">{errorMessage(pack.error)}</p>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

function PageTitle({ action }: { action?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
      <div>
        <div className="eyebrow mb-1 text-muted-light">MARKETING PACKS</div>
        <h1 className="text-ink text-2xl font-bold tracking-tight">一张好图，不止一种可能。</h1>
        <p className="text-muted mt-0.5 text-xs">
          从商品主图到营销海报，让每个投放位置都有合适的表达。
        </p>
      </div>
      {action}
    </header>
  )
}

function SessionSwitch({ currentId }: { currentId: string }) {
  const navigate = useNavigate()
  const { data: sessions = [] } = useSessions()
  if (sessions.length < 2) return null
  return (
    <select
      value={currentId}
      onChange={(event) => navigate(`/marketing/${event.target.value}`)}
      className="border-line text-ink rounded-control max-w-40 shrink-0 truncate border bg-paper px-2.5 py-1 text-xs"
      aria-label="切换会话"
    >
      {sessions.map((item) => (
        <option key={item.id} value={item.id}>
          {shortTitle(item.title)}
        </option>
      ))}
    </select>
  )
}
