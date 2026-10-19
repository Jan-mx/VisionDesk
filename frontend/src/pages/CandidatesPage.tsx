import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import type { Asset } from '@/api/assets'
import ProgressBar from '@/components/ui/ProgressBar'
import { useRun } from '@/hooks/useRun'
import { useCreateSession } from '@/hooks/useSessions'

/** 候选图挑选页（复现 creative-v1 方案 A · Candidates） */
export default function CandidatesPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const [picked, setPicked] = useState<string | null>(null)
  const { status, progress, stage, error, prompt, candidates, notFound } = useRun(runId || null)
  const createSession = useCreateSession()

  // 采用一张进入编辑，同批其余候选一并带进会话图片墙
  const adopt = () =>
    picked &&
    createSession.mutate(
      {
        current_asset_id: picked,
        asset_ids: candidates.map((asset) => asset.id),
        title: prompt ?? undefined,
      },
      { onSuccess: (session) => navigate(`/editor/${session.id}`) },
    )

  if (notFound) {
    return <Centered title="任务不存在" hint="链接可能已失效，回到创作页重新开始。" />
  }

  if (status === 'failed' || status === 'canceled') {
    return (
      <Centered title="生成失败" hint={error ?? '未知原因'}>
        <Link
          to="/create"
          className="bg-brand hover:bg-brand-strong rounded-control mt-5 px-5 py-2 text-sm font-semibold text-white shadow-xs"
        >
          返回重新创作
        </Link>
      </Centered>
    )
  }

  if (status !== 'succeeded') {
    return <Progress percent={progress} stage={stage} />
  }

  const selectedIndex = candidates.findIndex((c) => c.id === picked)

  return (
    <div className="mx-auto max-w-[1200px] px-6 py-8 sm:px-10">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="eyebrow mb-1 text-muted-light">FOUR POSSIBILITIES</div>
          <h1 className="text-ink text-2xl font-bold tracking-tight">你的灵感，有了多种模样。</h1>
          <p className="text-muted mt-1 text-xs">
            选一张最心动的进入画布打磨，其余候选图会留在会话图片墙随时切回。
          </p>
        </div>
        <Link
          to="/create"
          className="border-line bg-paper text-ink hover:bg-soft hover:border-line-strong rounded-control shadow-control flex items-center gap-1.5 border px-3.5 py-2 text-xs font-semibold transition-colors"
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path d="M19 12H5m6-6-6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <span>返回创作</span>
        </Link>
      </header>

      {/* 提示词摘要条 */}
      <div className="bg-lavender border-lavender-border rounded-card mb-6 flex flex-wrap items-center justify-between gap-4 border p-4 text-xs">
        <div className="flex items-center gap-3">
          <span className="text-brand text-base">✦</span>
          <div>
            <span className="text-ink font-semibold">{prompt || '商品视觉探索生成'}</span>
            <span className="text-muted block text-[11px] mt-0.5">
              真实任务产出 · {candidates.length} 张候选 · contain 构图保护
            </span>
          </div>
        </div>
        <span className="bg-mint text-[#2b6a48] border-mint-border flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold">
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path d="m5 12 4 4L19 6" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          已完成
        </span>
      </div>

      {/* 数量自适应候选网格 */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {candidates.map((asset, index) => (
          <Candidate
            key={asset.id}
            asset={asset}
            index={index}
            selected={picked === asset.id}
            onSelect={() => setPicked(asset.id)}
          />
        ))}
      </div>

      {/* 底部确认栏 */}
      <div className="border-line bg-paper shadow-card rounded-card mt-6 flex flex-wrap items-center justify-between gap-4 border p-4 sm:p-5">
        <div className="flex items-center gap-3">
          <span className="bg-brand-soft text-brand rounded-full px-3 py-1 text-xs font-bold">
            {selectedIndex >= 0 ? `已选 0${selectedIndex + 1}` : '未点选'}
          </span>
          <p className="text-muted text-xs">其余候选也会保留在编辑器的图片墙中，随时切换。</p>
        </div>

        <button
          type="button"
          disabled={!picked || createSession.isPending}
          onClick={adopt}
          className="bg-brand hover:bg-brand-strong rounded-control shadow-sm flex items-center gap-2 px-6 py-2.5 text-sm font-semibold text-white transition-all hover:shadow-md disabled:cursor-not-allowed disabled:opacity-40"
        >
          <span>{createSession.isPending ? '正在打开画布…' : '继续打磨这张图'}</span>
          <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
            <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      <p className="text-muted-light mt-5 flex items-center justify-center gap-1.5 text-center text-xs">
        <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
          <path d="m12 2 8 4v6q-1 7-8 10-7-3-8-10V6l8-4Zm-4 10 3 3 5-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span>保留原始素材，每一次尝试都可以放心开始。</span>
      </p>
    </div>
  )
}

function Candidate({
  asset,
  index,
  selected,
  onSelect,
}: {
  asset: Asset
  index: number
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`bg-paper rounded-card group relative block overflow-hidden border-2 text-left transition-all ${
        selected
          ? 'border-brand ring-4 ring-brand/10 shadow-card'
          : 'border-line hover:border-brand/70 hover:shadow-hover hover:-translate-y-1'
      }`}
    >
      <div className="relative flex h-[310px] w-full items-center justify-center bg-white p-2">
        <img
          src={asset.url}
          alt={`候选图 0${index + 1}`}
          loading="lazy"
          className="h-full w-full object-contain"
        />
        <span className="bg-paper/90 text-ink shadow-xs absolute top-3 left-3 rounded-md px-2 py-0.5 text-xs font-bold backdrop-blur-md">
          0{index + 1}
        </span>
        <span
          className={`absolute top-3 right-3 grid size-6 place-items-center rounded-full border transition-colors ${
            selected ? 'bg-brand border-brand text-white' : 'bg-paper/80 border-line text-transparent'
          }`}
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path d="m5 12 4 4L19 6" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </div>

      <div className="border-line/70 flex items-center justify-between border-t p-3 text-xs">
        <span className="text-ink font-semibold">候选 0{index + 1}</span>
        <span className="text-muted text-[11px]">{selected ? '当前选中' : '点击挑选'}</span>
      </div>
    </button>
  )
}

function Progress({ percent, stage }: { percent: number; stage: string }) {
  return (
    <Centered title="正在生成候选画面" hint={stage || '任务已提交，正在调度扩散模型生成…'}>
      <ProgressBar value={percent} className="mt-6 h-1.5 w-72 rounded-full" />
      <p className="text-muted mt-2 text-xs tabular-nums">{percent}%</p>
    </Centered>
  )
}

function Centered({
  title,
  hint,
  children,
}: {
  title: string
  hint: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex h-full min-h-[460px] flex-col items-center justify-center px-8 text-center">
      <h1 className="text-ink text-xl font-bold">{title}</h1>
      <p className="text-muted mt-1.5 max-w-md text-xs">{hint}</p>
      {children}
    </div>
  )
}
