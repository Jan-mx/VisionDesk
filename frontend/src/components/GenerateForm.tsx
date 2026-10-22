import { useState } from 'react'

import { RATIO_LABELS, type GenerateInput, type Ratio } from '@/api/runs'

const RATIOS = Object.keys(RATIO_LABELS) as Ratio[]
const COUNTS = [1, 2, 4, 6]

const SCENARIOS = [
  {
    label: '商品主图',
    icon: 'M5 7h14l2 14H3L5 7ZM8 8V5a4 4 0 0 1 8 0v3',
    suffix: '商品主图，纯白背景，居中构图，柔和顶光，高清质感',
  },
  {
    label: '场景氛围图',
    icon: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2',
    suffix: '场景氛围图，温暖木质桌面，侧逆光、浅景深，自然光感',
  },
  {
    label: '模特上身',
    icon: 'M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2',
    suffix: '模特展示，双手捧物，自然居家光影，突出商品细节与真实比例',
  },
  {
    label: '促销海报',
    icon: 'M5 3h14v18H5zM8 7h8M8 11h5m-5 4h8m-8 3h5',
    suffix: '促销海报，商品主体微倾，预留排版安全文案区，大促氛围',
  },
  {
    label: '多尺寸物料',
    icon: 'M3 9V3h6m6 0h6v6M3 15v6h6m6 0h6v-6',
    suffix: '多尺寸物料，主体完整居中，适配 1:1、4:5、9:16 多比例投放',
  },
]

export default function GenerateForm({
  onSubmit,
  pending,
  defaultPrompt = '',
}: {
  onSubmit: (input: GenerateInput) => void
  pending: boolean
  defaultPrompt?: string
}) {
  const [prompt, setPrompt] = useState(defaultPrompt)
  const [ratio, setRatio] = useState<Ratio>('1:1')
  const [count, setCount] = useState(4)
  const [negative, setNegative] = useState('')
  const [advanced, setAdvanced] = useState(false)

  const canSubmit = prompt.trim().length > 0 && !pending

  const appendScenario = (suffix: string) => {
    setPrompt((prev) => {
      const trimmed = prev.trim()
      if (!trimmed) return `创作一张${suffix}。`
      if (trimmed.endsWith('。') || trimmed.endsWith('，') || trimmed.endsWith(',')) {
        return `${trimmed}${suffix}。`
      }
      return `${trimmed}，${suffix}。`
    })
  }

  return (
    <div className="w-full">
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (!canSubmit) return
          onSubmit({
            prompt: prompt.trim(),
            ratio,
            count,
            negative_prompt: negative.trim() || undefined,
          })
        }}
        className="border-line bg-paper shadow-card hover:shadow-hover rounded-card focus-within:border-brand/70 focus-within:ring-4 focus-within:ring-brand/10 border p-4.5 text-left transition-all"
      >
        <textarea
          rows={4}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            }
          }}
          placeholder="描述你想要的画面，例如：一瓶植萃精华，置于自然光下，鼠尾草绿背景，清新、有质感……"
          aria-label="画面描述"
          className="text-ink placeholder:text-muted-light w-full resize-none bg-transparent px-1 pt-1 pb-2 text-[14.5px] leading-relaxed outline-none"
        />

        <div className="border-line/60 flex flex-wrap items-center justify-between gap-3 border-t pt-3.5">
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              label="比例"
              options={RATIOS.map((value) => ({ value, label: value }))}
              value={ratio}
              onChange={setRatio}
            />
            <Segmented
              label="数量"
              options={COUNTS.map((value) => ({ value, label: `${value} 张` }))}
              value={count}
              onChange={setCount}
            />

            <button
              type="button"
              onClick={() => setAdvanced((open) => !open)}
              className="text-muted hover:text-ink hover:bg-soft rounded-control px-2.5 py-1.5 text-xs font-medium transition-colors"
            >
              {advanced ? '收起排除项 ▴' : '排除项 ▾'}
            </button>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-muted-light hidden text-xs sm:inline">
              {pending ? '任务已提交，稍后进入候选页…' : 'Enter 生成 · Shift+Enter 换行'}
            </span>
            <button
              type="submit"
              disabled={!canSubmit}
              className="bg-brand hover:bg-brand-strong rounded-control shadow-sm flex min-h-[38px] items-center gap-2 px-5 py-2 text-sm font-semibold text-white transition-all hover:shadow-md disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                <path
                  d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
              <span>{pending ? '正在生成…' : '开始创作'}</span>
              <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </div>

        {advanced && (
          <div className="border-line animate-fade-in mt-3 border-t pt-3">
            <input
              value={negative}
              onChange={(event) => setNegative(event.target.value)}
              placeholder="不希望出现的内容，例如：文字、水印、多余的手、变形"
              aria-label="排除项"
              className="border-line bg-soft text-ink placeholder:text-muted-light focus:border-brand rounded-control w-full border px-3 py-2 text-xs outline-none"
            />
          </div>
        )}
      </form>

      {/* 场景灵感追加标签 */}
      <div className="mt-4 flex flex-wrap justify-center gap-2">
        {SCENARIOS.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={() => appendScenario(item.suffix)}
            className="border-line bg-paper text-ink-secondary hover:border-brand hover:text-brand hover:bg-brand-soft shadow-xs flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs font-medium transition-all"
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-3.5 text-brand" aria-hidden>
              <path d={item.icon} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
            <span>{item.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div className="border-line bg-soft rounded-control flex items-center gap-0.5 border p-0.5">
      <span className="text-muted-light px-1.5 text-[11px] font-medium">{label}</span>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={option.value === value}
          className={`rounded-[7px] px-2.5 py-1 text-xs transition-all ${
            option.value === value
              ? 'bg-paper text-brand font-semibold shadow-xs'
              : 'text-muted hover:text-ink'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
