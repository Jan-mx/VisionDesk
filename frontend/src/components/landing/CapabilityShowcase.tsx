import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { CandidatesPreview, ExportsPreview, LayersPreview, SelectionPreview } from './previews'

const PRODUCTS = [
  {
    id: 'serum',
    name: '植萃精华 · 自然有光',
    sub: '商品主图 / 场景氛围',
    tag: '美妆护肤',
    img: '/creative/serum.webp',
  },
  {
    id: 'sneaker',
    name: '轻盈每一步',
    sub: '鞋服配饰 / 品牌海报',
    tag: '鞋服配饰',
    img: '/creative/sneaker.webp',
  },
  {
    id: 'tote',
    name: '把好心情装进口袋',
    sub: '生活方式 / 商品主图',
    tag: '生活好物',
    img: '/creative/tote.webp',
  },
  {
    id: 'coffee',
    name: '留一点时间，给自己',
    sub: '家居日用 / 营销物料',
    tag: '家居日用',
    img: '/creative/coffee.webp',
  },
]

const CAPABILITIES: { title: string; desc: string; preview: ReactNode }[] = [
  {
    title: '一句话生成',
    desc: '无需上传图片，描述需求即可拿到 4 个候选方向，挑中的那张直接进入编辑。',
    preview: <CandidatesPreview />,
  },
  {
    title: '主体级编辑',
    desc: '点选画面里的任意物体后再下指令，改动被约束在选区内，不会外溢到其他区域。',
    preview: <SelectionPreview />,
  },
  {
    title: '语义图层',
    desc: '主体、背景、文字自动分层，任意物体也可按需独立成层，随时回到上一版重做。',
    preview: <LayersPreview />,
  },
  {
    title: '物料包交付',
    desc: '卖点标注与 1:1、4:5、9:16 尺寸一次导出，主体不裁切，直接上架投放。',
    preview: <ExportsPreview />,
  },
]

/**
 * 落地页展示模块：
 * 1. 案例画廊展台（复现 creative-v1 方案 A · Showcase）
 * 2. 核心能力拆解卡（复用 previews.tsx 示意图，适配方案 A 视觉风格）
 */
export default function CapabilityShowcase() {
  return (
    <section className="mx-auto max-w-[1200px] px-6 pt-8 pb-10">
      {/* 1. 上段：案例画廊 */}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-ink text-[22px] font-bold tracking-tight">好想法，值得被看见。</h2>
          <p className="text-muted mt-1 text-xs">从一张商品图开始，探索更多可能。</p>
        </div>
        <Link
          to="/marketing"
          className="text-muted hover:text-brand flex items-center gap-1.5 text-xs font-semibold transition-colors"
        >
          <span>探索创作方式</span>
          <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
            <path d="M5 12h14m-6-6 6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </Link>
      </div>

      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {PRODUCTS.map((p) => (
          <Link
            key={p.id}
            to="/create"
            className="border-line bg-paper shadow-card hover:shadow-hover hover:-translate-y-1 group relative block overflow-hidden rounded-[18px] border transition-all"
          >
            <div className="relative h-[220px] overflow-hidden bg-[#f7f6f4]">
              <img src={p.img} alt={p.name} className="h-full w-full object-cover object-center" />
              <span className="bg-paper/90 shadow-xs text-ink-secondary absolute top-3 left-3 rounded-full px-2.5 py-1 text-[10.5px] font-semibold backdrop-blur-md">
                {p.tag}
              </span>
            </div>

            <div className="flex items-center justify-between gap-3 p-4">
              <div>
                <strong className="text-ink group-hover:text-brand block text-[13.5px] font-semibold transition-colors">
                  {p.name}
                </strong>
                <p className="text-muted mt-1 text-[11px]">{p.sub}</p>
              </div>
              <svg
                viewBox="0 0 24 24"
                fill="none"
                className="text-muted group-hover:text-brand size-4 shrink-0 transition-transform group-hover:translate-x-1"
                aria-hidden
              >
                <path d="M5 12h14m-6-6 6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </div>
          </Link>
        ))}
      </div>

      {/* 优雅过渡分隔线 */}
      <div className="border-line/70 my-12 border-t sm:my-14" />

      {/* 2. 下段：恢复核心能力展示卡 */}
      <div className="mb-6">
        <span className="bg-lavender text-brand inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold shadow-xs">
          <span>✦</span>
          <span>核心能力</span>
        </span>
        <h2 className="text-ink text-[22px] sm:text-2xl font-bold tracking-tight mt-2.5">
          不只是生成一张好看的图
        </h2>
        <p className="text-muted text-xs sm:text-[13px] mt-1 max-w-xl leading-relaxed">
          从候选方向到局部精修，再到分层与多尺寸交付，整条链路都在同一个工作台里完成。
        </p>
      </div>

      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {CAPABILITIES.map((item) => (
          <article
            key={item.title}
            className="border-line bg-paper rounded-card shadow-card hover:shadow-hover hover:-translate-y-1 flex flex-col border p-5 transition-all"
          >
            <div className="border-line/70 bg-canvas/70 flex h-40 items-center justify-center overflow-hidden rounded-[14px] border p-3">
              {item.preview}
            </div>
            <h3 className="text-ink text-[14.5px] font-bold mt-4">{item.title}</h3>
            <p className="text-muted text-xs leading-relaxed mt-1.5">{item.desc}</p>
          </article>
        ))}
      </div>
    </section>
  )
}
