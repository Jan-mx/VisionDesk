import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import type { Asset } from '@/api/assets'
import { isTerminal } from '@/api/runs'
import ImageDropzone from '@/components/ImageDropzone'
import ProgressBar from '@/components/ui/ProgressBar'
import { useCreateBatch, useBatch, useBatches, useExportBatch } from '@/hooks/useBatch'
import { errorMessage } from '@/hooks/useAuth'
import { useAssetLibrary, useUploadAsset } from '@/hooks/useAssets'
import { formatDateTime } from '@/lib/format'
import {
  BATCH_STEPS,
  EMPTY_DRAFT,
  EXPAND_RATIOS,
  type BatchDraft,
  type BatchTool,
  draftReady,
  operationsOf,
} from '@/lib/batch'
import { toast } from '@/stores/toasts'

const MAX_IMAGES = 20

/** 批量处理页（复现 creative-v1 方案 A · Batch 1.2:1 双栏规范） */
export default function BatchPage() {
  const { runId = '' } = useParams()
  return runId ? <Watch runId={runId} /> : <Compose />
}

function Compose() {
  const navigate = useNavigate()
  const create = useCreateBatch()
  const upload = useUploadAsset()
  const { data: groups = [] } = useAssetLibrary()
  const { data: history = [] } = useBatches()
  const [assets, setAssets] = useState<Asset[]>([])
  const [draft, setDraft] = useState<BatchDraft>(EMPTY_DRAFT)
  const blocked = draftReady(draft, assets.length)

  const add = (asset: Asset) =>
    setAssets((current) =>
      current.some((item) => item.id === asset.id) || current.length >= MAX_IMAGES
        ? current
        : [...current, asset],
    )

  const start = () => {
    if (blocked) return
    create.mutate(
      {
        asset_ids: assets.map((asset) => asset.id),
        operations: operationsOf(draft),
        formats: draft.formats,
      },
      { onSuccess: (run) => navigate(`/batch/${run.id}`) },
    )
  }

  return (
    <div className="mx-auto max-w-[1220px] px-6 py-8 sm:px-10">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="eyebrow mb-1 text-muted-light">BATCH PIPELINE</div>
          <h1 className="text-ink text-2xl font-bold tracking-tight">好看的事，一起完成。</h1>
          <p className="text-muted mt-0.5 text-xs">
            对多张图走同一套像素处理：去背景、换背景、调色、超分、扩图、投放尺寸。
          </p>
        </div>
      </div>

      {/* 1.2:1 桌面双栏布局 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1.2fr_1fr] items-start">
        {/* 左栏：资产挑选与素材库 */}
        <section className="border-line bg-paper rounded-card shadow-card border p-6 flex flex-col gap-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <span className="bg-lavender text-brand font-bold text-xs size-6 rounded-md grid place-items-center">
                01
              </span>
              <h2 className="text-ink text-sm font-bold">选好这一批图片</h2>
            </div>
            <span className="bg-brand-soft text-brand rounded-full px-3 py-0.5 text-xs font-semibold">
              {assets.length} 张已选
            </span>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {assets.map((asset) => (
              <div
                key={asset.id}
                className="border-line bg-paper shadow-xs relative flex flex-col overflow-hidden rounded-[10px] border p-1"
              >
                <img src={asset.url} alt="" className="h-28 w-full object-cover rounded" />
                <button
                  type="button"
                  onClick={() => setAssets((current) => current.filter((item) => item.id !== asset.id))}
                  className="bg-ink/70 hover:bg-danger absolute -top-1 -right-1 grid size-5 place-items-center rounded-full text-[10px] text-white shadow-xs"
                  aria-label="移除图片"
                >
                  ×
                </button>
                <span className="text-muted mt-1 block truncate text-center text-[10.5px]">
                  {asset.width}×{asset.height}
                </span>
              </div>
            ))}

            <div className="col-span-full">
              <ImageDropzone
                multiple
                disabled={upload.isPending || assets.length >= MAX_IMAGES}
                hint={`JPG / PNG / WebP，最多 ${MAX_IMAGES} 张`}
                onFile={(file) => upload.mutate(file, { onSuccess: add })}
              />
            </div>
          </div>

          {groups.length > 0 && (
            <div className="border-line/70 border-t pt-4">
              <p className="text-muted text-xs font-medium mb-2.5">从历史素材库快速勾选添加：</p>
              <div className="flex flex-wrap gap-2">
                {groups
                  .flatMap((group) => group.assets)
                  .slice(0, 12)
                  .map((asset) => (
                    <button
                      key={asset.id}
                      type="button"
                      onClick={() => add(asset)}
                      className="border-line hover:border-brand size-14 shrink-0 overflow-hidden rounded-[10px] border transition-transform hover:scale-105"
                      title="点击添加到批处理"
                    >
                      <img src={asset.url} alt="" className="size-full object-cover" />
                    </button>
                  ))}
              </div>
            </div>
          )}

          <div className="bg-soft border-line/60 rounded-control flex items-center gap-3 border p-3.5 text-xs text-muted">
            <svg viewBox="0 0 24 24" fill="none" className="size-4 text-brand shrink-0" aria-hidden>
              <path d="m12 2 8 4v6q-1 7-8 10-7-3-8-10V6l8-4Zm-4 10 3 3 5-6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
            </svg>
            <span>放心尝试，原图始终保留。批处理结果独立生成，不覆盖原始素材。</span>
          </div>
        </section>

        {/* 右栏：处理流水线步骤安排 */}
        <section className="border-line bg-paper rounded-card shadow-card border p-6 flex flex-col gap-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <span className="bg-lavender text-brand font-bold text-xs size-6 rounded-md grid place-items-center">
                02
              </span>
              <h2 className="text-ink text-sm font-bold">安排好处理步骤</h2>
            </div>
            <span className="text-muted text-[11px]">按流水线顺序执行</span>
          </div>

          <Pipeline draft={draft} onChange={setDraft} />

          <div className="border-line/80 border-t pt-4">
            <button
              type="button"
              disabled={Boolean(blocked) || create.isPending}
              onClick={start}
              className="bg-brand hover:bg-brand-strong rounded-control shadow-sm flex w-full items-center justify-center gap-2 py-3 text-sm font-semibold text-white transition-all hover:shadow-md disabled:cursor-not-allowed disabled:opacity-40"
            >
              <span>{create.isPending ? '正在提交流水线…' : `开始批量处理 (${assets.length} 张)`}</span>
              <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            {blocked && <p className="text-faint mt-2 text-center text-xs">{blocked}</p>}
            {create.isError && <p className="text-danger mt-2 text-center text-xs">{errorMessage(create.error)}</p>}
          </div>
        </section>
      </div>

      {/* 底部最近任务列表 */}
      {history.length > 0 && (
        <section className="mt-10">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-ink text-base font-bold">最近任务</h2>
            <span className="text-muted text-xs">执行记录</span>
          </div>

          <ul className="space-y-2.5">
            {history.map((item) => (
              <li key={item.run.id}>
                <Link
                  to={`/batch/${item.run.id}`}
                  className="border-line bg-paper shadow-sm hover:border-brand/70 hover:shadow-hover flex items-center justify-between rounded-card border p-4 transition-all"
                >
                  <div className="flex items-center gap-3">
                    <span className="bg-mint text-[#2b6a48] border-mint-border rounded-full border px-2.5 py-0.5 text-xs font-semibold">
                      {item.run.stage || item.run.status}
                    </span>
                    <strong className="text-ink text-sm font-semibold">
                      {item.items.length} 张图片流水线
                    </strong>
                  </div>
                  <span className="text-faint font-mono text-xs tabular-nums">
                    {formatDateTime(item.created_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function Pipeline({
  draft,
  onChange,
}: {
  draft: BatchDraft
  onChange: (draft: BatchDraft) => void
}) {
  const toggle = (id: BatchTool) => {
    const selected = draft.selected.includes(id)
      ? draft.selected.filter((item) => item !== id)
      : [...draft.selected, id]
    onChange({ ...draft, selected })
  }

  const toggleFormat = (fmt: 'png' | 'jpg') => {
    const formats = draft.formats.includes(fmt)
      ? draft.formats.filter((item) => item !== fmt)
      : [...draft.formats, fmt]
    onChange({ ...draft, formats })
  }

  const toggleDelivery = (ratio: '1:1' | '4:5' | '9:16') => {
    const delivery = draft.delivery.includes(ratio)
      ? draft.delivery.filter((item) => item !== ratio)
      : [...draft.delivery, ratio]
    onChange({ ...draft, delivery })
  }

  return (
    <div className="flex flex-col gap-2.5">
      {BATCH_STEPS.map((step, idx) => {
        const on = draft.selected.includes(step.id)
        return (
          <div
            key={step.id}
            className={`border rounded-control p-3.5 transition-all ${
              on ? 'border-brand/60 bg-brand-soft/50 shadow-xs' : 'border-line bg-paper'
            }`}
          >
            <label className="flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                checked={on}
                onChange={() => toggle(step.id)}
                className="mt-0.5"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between">
                  <span className="text-ink text-xs font-bold">{step.label}</span>
                  <span className="text-muted font-mono text-[10.5px]">0{idx + 1}</span>
                </div>
                <span className="text-muted mt-0.5 block text-[11px]">{step.hint}</span>
              </div>
            </label>

            {on && step.id === 'replace_background' && (
              <div className="mt-2.5 space-y-2 border-t border-brand/20 pt-2">
                <input
                  value={draft.prompt}
                  onChange={(event) => onChange({ ...draft, prompt: event.target.value })}
                  placeholder="例如：浅木色桌面，晨光，自然质感"
                  className="border-line bg-paper text-ink rounded-control w-full border px-3 py-1.5 text-xs outline-none"
                />
                <div className="flex items-center gap-1.5">
                  <span className="text-muted text-[11px]">每张候选：</span>
                  {[1, 2, 3, 4].map((count) => (
                    <button
                      key={count}
                      type="button"
                      onClick={() => onChange({ ...draft, count })}
                      className={`rounded-control px-2.5 py-1 text-xs font-medium ${
                        draft.count === count
                          ? 'bg-brand text-white font-semibold'
                          : 'border-line text-muted border bg-paper'
                      }`}
                    >
                      {count}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {on && step.id === 'adjust_image' && (
              <div className="mt-2.5 grid grid-cols-3 gap-2 border-t border-brand/20 pt-2">
                <Slider
                  label="亮度"
                  value={draft.brightness}
                  onChange={(brightness) => onChange({ ...draft, brightness })}
                />
                <Slider
                  label="对比"
                  value={draft.contrast}
                  onChange={(contrast) => onChange({ ...draft, contrast })}
                />
                <Slider
                  label="饱和"
                  value={draft.saturation}
                  onChange={(saturation) => onChange({ ...draft, saturation })}
                />
              </div>
            )}

            {on && step.id === 'upscale_image' && (
              <div className="mt-2.5 flex gap-2 border-t border-brand/20 pt-2">
                {([2, 4] as const).map((scale) => (
                  <button
                    key={scale}
                    type="button"
                    onClick={() => onChange({ ...draft, scale })}
                    className={`rounded-control px-3 py-1 text-xs font-semibold ${
                      draft.scale === scale ? 'bg-brand text-white' : 'border-line text-muted border bg-paper'
                    }`}
                  >
                    {scale} 倍超分
                  </button>
                ))}
              </div>
            )}

            {on && step.id === 'expand_canvas' && (
              <div className="mt-2.5 flex flex-wrap gap-2 border-t border-brand/20 pt-2">
                {EXPAND_RATIOS.map((ratio) => (
                  <button
                    key={ratio}
                    type="button"
                    onClick={() => onChange({ ...draft, expandRatio: ratio })}
                    className={`rounded-control px-2.5 py-1 text-xs font-medium ${
                      draft.expandRatio === ratio ? 'bg-brand text-white font-semibold' : 'border-line text-muted border bg-paper'
                    }`}
                  >
                    {ratio}
                  </button>
                ))}
              </div>
            )}

            {on && step.id === 'prepare_delivery_sizes' && (
              <div className="mt-2.5 flex flex-wrap gap-2 border-t border-brand/20 pt-2">
                {(['1:1', '4:5', '9:16'] as const).map((ratio) => (
                  <label key={ratio} className="flex items-center gap-1.5 text-xs text-ink cursor-pointer">
                    <input
                      type="checkbox"
                      checked={draft.delivery.includes(ratio)}
                      onChange={() => toggleDelivery(ratio)}
                    />
                    <span>{ratio}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )
      })}

      <div className="mt-3 flex items-center justify-between border-t border-line pt-3 text-xs">
        <span className="text-muted font-medium">输出格式</span>
        <div className="flex items-center gap-4 text-ink">
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" checked={draft.formats.includes('png')} onChange={() => toggleFormat('png')} />
            <span>PNG</span>
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" checked={draft.formats.includes('jpg')} onChange={() => toggleFormat('jpg')} />
            <span>JPG</span>
          </label>
        </div>
      </div>
    </div>
  )
}

function Slider({
  label,
  value,
  onChange,
}: {
  label: string
  value: number
  onChange: (value: number) => void
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between text-[11px] text-muted">
        <span>{label}</span>
        <span className="font-mono text-ink">{value > 0 ? `+${value}` : value}</span>
      </div>
      <input
        type="range"
        min={-50}
        max={50}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </div>
  )
}

function Watch({ runId }: { runId: string }) {
  const { data, live, isError } = useBatch(runId)
  const pack = useExportBatch(runId)
  const done = live.status && isTerminal(live.status)
  const succeeded = data?.items.filter((item) => item.status === 'succeeded').length ?? 0

  const download = () =>
    pack.mutate(undefined, {
      onSuccess: ({ blob, filename }) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        link.click()
        URL.revokeObjectURL(url)
        toast(`已打包 ${succeeded} 张图片`)
      },
    })

  if (!data && isError) {
    return (
      <div className="flex h-full min-h-[460px] flex-col items-center justify-center px-8 text-center">
        <h1 className="text-ink text-lg font-bold">任务不存在</h1>
        <Link to="/batch" className="text-brand mt-4 text-xs font-semibold">
          返回批量处理
        </Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-8 sm:px-10">
      <div className="border-line bg-paper rounded-card shadow-card mb-8 border p-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <span className="bg-mint text-[#2b6a48] border-mint-border rounded-full border px-2.5 py-0.5 text-xs font-semibold">
            {done ? '流水线已完成' : '流水线执行中'}
          </span>
          <h1 className="text-ink text-2xl font-bold tracking-tight mt-2">
            批量处理进度
          </h1>
          <p className="text-muted mt-1 text-xs">
            {live.stage || '处理中'}
            {done ? '' : ` · ${live.progress}%`}
          </p>
          {!done && (
            <ProgressBar value={live.progress} className="mt-3 h-1 w-64 rounded-full" />
          )}
        </div>
        <div className="flex items-center gap-3">
          <Link
            to="/batch"
            className="border-line text-muted hover:text-ink hover:bg-soft rounded-control border px-3.5 py-2 text-xs font-semibold"
          >
            新建批量
          </Link>
          <button
            type="button"
            disabled={!done || succeeded === 0 || pack.isPending}
            onClick={download}
            className="bg-brand hover:bg-brand-strong rounded-control shadow-sm flex items-center gap-1.5 px-4.5 py-2 text-xs font-semibold text-white transition-all disabled:opacity-40"
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
              <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>{pack.isPending ? '正在打包…' : `打包下载 (${succeeded} 张)`}</span>
          </button>
        </div>
      </div>

      {live.error && <p className="text-danger mb-4 text-xs">{live.error}</p>}
      {pack.isError && <p className="text-danger mb-4 text-xs">{errorMessage(pack.error)}</p>}

      {/* 逐图处理结果卡片 */}
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data?.items.map((item, idx) => (
          <li
            key={item.source.id || idx}
            className="border-line bg-paper rounded-card shadow-sm border p-3.5 flex items-center gap-3"
          >
            <div className="border-line size-16 shrink-0 overflow-hidden rounded-[8px] border bg-canvas">
              {item.outputs[0]?.url ? (
                <img src={item.outputs[0].url} alt="" className="size-full object-contain" />
              ) : (
                <img src={item.source.url} alt="" className="size-full object-contain opacity-60" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <span className={`text-[11px] font-semibold ${item.status === 'succeeded' ? 'text-success' : 'text-danger'}`}>
                {item.status === 'succeeded' ? '处理成功' : item.status}
              </span>
              <p className="text-muted mt-0.5 text-xs truncate">
                产出 {item.outputs.length} 张图片
              </p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
