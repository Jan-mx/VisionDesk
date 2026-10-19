import { Link } from 'react-router-dom'

const PRODUCTS = [
  {
    id: 'serum',
    name: '植萃精华 · 自然有光',
    sub: '商品主图 / 场景氛围',
    tag: '美妆护肤',
    img: '/creative/serum.svg',
  },
  {
    id: 'sneaker',
    name: '轻盈每一步',
    sub: '鞋服配饰 / 品牌海报',
    tag: '鞋服配饰',
    img: '/creative/sneaker.svg',
  },
  {
    id: 'tote',
    name: '把好心情装进口袋',
    sub: '生活方式 / 商品主图',
    tag: '生活好物',
    img: '/creative/tote.svg',
  },
  {
    id: 'coffee',
    name: '留一点时间，给自己',
    sub: '家居日用 / 营销物料',
    tag: '家居日用',
    img: '/creative/coffee.svg',
  },
]

/** 案例画廊展台（复现 creative-v1 方案 A · Showcase） */
export default function CapabilityShowcase() {
  return (
    <section className="mx-auto max-w-[1200px] px-6 pt-8 pb-12">
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
            <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
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
                <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </div>
          </Link>
        ))}
      </div>
    </section>
  )
}
