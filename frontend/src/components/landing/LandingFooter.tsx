import BrandMark from '@/components/BrandMark'

export default function LandingFooter() {
  return (
    <footer className="border-line/70 border-t bg-canvas">
      <div className="text-muted-light mx-auto flex max-w-[1200px] flex-col items-center justify-between gap-4 px-6 py-6 text-xs sm:flex-row">
        <BrandMark size="sm">
          <span className="text-muted text-xs font-semibold">VisionDesk 智绘台 · 让创意，自然发生。</span>
        </BrandMark>
        <p>© 2026 VisionDesk · 为电商运营与内容创作者打造的 AI 视觉工作台</p>
      </div>
    </footer>
  )
}
