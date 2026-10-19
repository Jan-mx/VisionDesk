import { Link } from 'react-router-dom'

/** 行动号召横幅（复现 creative-v1 方案 A · CTA Banner） */
export default function StartBanner({ label: _label, to }: { label: string; to: string }) {
  return (
    <section className="mx-auto max-w-[1200px] px-6 pt-4 pb-14">
      <div className="bg-lavender border-lavender-border rounded-card flex flex-col items-start justify-between gap-6 border px-8 py-8 sm:flex-row sm:items-center sm:px-10">
        <div>
          <h2 className="text-ink text-[22px] font-extrabold tracking-tight sm:text-2xl">
            下一张好图，从你的灵感开始。
          </h2>
          <p className="mt-1.5 text-xs text-[#6e6b8a]">
            商品主图、场景氛围、营销海报，都在同一个工作台。
          </p>
        </div>

        <Link
          to={to}
          className="bg-brand hover:bg-brand-strong rounded-control flex items-center gap-2 px-6 py-3 text-sm font-semibold text-white shadow-sm transition-all hover:shadow-md"
        >
          <span>开启我的创作</span>
          <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
            <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      </div>
    </section>
  )
}
