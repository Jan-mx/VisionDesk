import { useRef, useState } from 'react'

import PromptComposer from './PromptComposer'

const SCENARIOS = [
  {
    label: '商品主图',
    icon: 'M5 7h14l2 14H3L5 7ZM8 8V5a4 4 0 0 1 8 0v3',
    prompt: '一瓶植萃精华，置于纯白背景上，居中构图，柔和顶光，高清质感，输出 1:1 主图。',
  },
  {
    label: '场景氛围图',
    icon: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2',
    prompt: '把商品放到温暖的北欧木质桌面，侧逆光、浅景深，窗外透出晨光，保留商品原有材质与颜色。',
  },
  {
    label: '模特上身',
    icon: 'M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2',
    prompt: '生成模特双手捧着该商品的半身展示图，简洁室内背景、柔和自然光，突出商品细节。',
  },
  {
    label: '促销海报',
    icon: 'M5 3h14v18H5zM8 7h8M8 11h5m-5 4h8m-8 3h5',
    prompt: '做一张大促海报：主标题「春季新品」，副标题「限时特惠」，商品在左下微倾，右侧留出排版文字区。',
  },
  {
    label: '多尺寸物料',
    icon: 'M3 9V3h6m6 0h6v6M3 15v6h6m6 0h6v-6',
    prompt: '基于这张商品图输出一套投放物料，包含 1:1、4:5、9:16 三个尺寸，主体不被裁切。',
  },
]

export default function LandingHero({
  initialPrompt,
  submitLabel,
  onStart,
}: {
  initialPrompt: string
  submitLabel: string
  onStart: (prompt: string) => void
}) {
  const [prompt, setPrompt] = useState(
    initialPrompt || '一瓶植萃精华，置于自然光下，鼠尾草绿背景，清新、有质感……'
  )
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const pick = (scenario: typeof SCENARIOS[0]) => {
    setPrompt(scenario.prompt)
    inputRef.current?.focus()
  }

  return (
    <section className="relative isolate overflow-hidden pt-10 pb-8 text-center sm:pt-14 sm:pb-12">
      {/* 顶部柔和环境光晕与淡点阵 */}
      <div className="bg-glow absolute inset-x-0 -top-24 h-[600px] -z-10" aria-hidden />

      <div className="relative mx-auto max-w-[1200px] px-6">
        {/* Kicker 小胶囊 */}
        <div
          className="bg-paper border-line text-ink-secondary shadow-xs animate-rise inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-xs font-medium"
          style={{ animationDelay: '50ms' }}
        >
          <span className="bg-lavender text-brand grid size-5 place-items-center rounded-md text-[11px]">
            ✦
          </span>
          <span>给每一个有想法的你</span>
        </div>

        {/* 54px 大标题配蜜桃橙手绘微弧 */}
        <h1
          className="text-ink animate-rise mt-5 text-[32px] font-extrabold tracking-[-1.5px] sm:text-[46px] lg:text-[54px] leading-[1.25]"
          style={{ animationDelay: '140ms' }}
        >
          让好商品，
          <span className="relative mx-1 inline-block whitespace-nowrap">
            <span className="text-brand">自带吸引力。</span>
            <span
              className="bg-accent/70 absolute inset-x-0 -bottom-1 h-2 -z-10 -rotate-1 rounded-full"
              aria-hidden
            />
          </span>
        </h1>

        <p
          className="text-muted animate-rise mx-auto mt-4 max-w-xl text-[14.5px] leading-relaxed"
          style={{ animationDelay: '220ms' }}
        >
          从一句灵感，到一整套商品物料。<br className="hidden sm:inline" />
          你的创意，在这里自然发生。
        </p>

        {/* 核心中央输入卡与两侧拍立得倾斜卡片 */}
        <div className="animate-rise relative mx-auto mt-8 max-w-[1100px]" style={{ animationDelay: '300ms' }}>
          {/* 左侧拍立得卡片 (植萃精华) + 悬浮呼吸 */}
          <div
            className="pointer-events-none absolute -left-2 top-2 hidden w-[130px] -rotate-[10deg] overflow-hidden rounded-[12px] border-[7px] border-b-[24px] border-white shadow-lift lg:block animate-float-left"
            aria-hidden
          >
            <img src="/creative/serum.webp" alt="" className="h-[150px] w-full object-cover" />
          </div>
          <span className="pointer-events-none absolute top-4 left-36 hidden text-[38px] text-[#e2a46a] lg:block select-none" aria-hidden>
            ✳
          </span>

          {/* 740px 输入卡片 */}
          <div className="relative z-10 mx-auto max-w-[740px]">
            <PromptComposer
              value={prompt}
              onChange={setPrompt}
              onSubmit={() => onStart(prompt)}
              onAttach={() => onStart(prompt)}
              submitLabel={submitLabel}
              placeholder="描述你想要的画面，比如：一瓶植萃精华，置于柔和晨光下，象牙白展台，通透、有质感……"
              inputRef={inputRef}
            />
          </div>

          {/* 右侧拍立得卡片 (轻盈跑鞋) + 悬浮呼吸 */}
          <div
            className="pointer-events-none absolute -right-2 top-8 hidden w-[130px] rotate-[9deg] overflow-hidden rounded-[12px] border-[7px] border-b-[24px] border-white shadow-lift lg:block animate-float-right"
            aria-hidden
          >
            <img src="/creative/sneaker.webp" alt="" className="h-[150px] w-full object-cover" />
          </div>
          <span className="pointer-events-none absolute top-28 right-36 hidden size-7 rounded-full border-4 border-[#a1d5bc] -rotate-12 lg:block" aria-hidden />
        </div>

        {/* 场景标签胶囊行 */}
        <div className="animate-rise mt-6 flex flex-wrap justify-center gap-2.5" style={{ animationDelay: '400ms' }}>
          {SCENARIOS.map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => pick(item)}
              className="border-line bg-paper text-ink hover:border-brand hover:text-brand hover:bg-brand-soft shadow-xs flex items-center gap-2 rounded-full border px-3.5 py-2 text-xs font-medium transition-all"
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-4 text-brand shrink-0" aria-hidden>
                <path d={item.icon} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>{item.label}</span>
            </button>
          ))}
        </div>

        {/* 底部轻微说明 */}
        <p className="text-muted-light animate-rise mt-5 text-xs" style={{ animationDelay: '480ms' }}>
          无需设计经验 &nbsp; · &nbsp; 从商品主图到营销海报 &nbsp; · &nbsp; 创作与编辑，一站完成
        </p>
      </div>
    </section>
  )
}
