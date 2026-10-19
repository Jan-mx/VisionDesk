import type { ReactNode } from 'react'

const GLYPH_SIZE = {
  sm: 'size-8 rounded-[10px]',
  md: 'size-9 rounded-[11px]',
} as const

/** 品牌标识：复用 creative-v1 原创几何电光蓝徽章与精致字标。 */
export default function BrandMark({
  size = 'md',
  className = '',
  children,
}: {
  size?: keyof typeof GLYPH_SIZE
  className?: string
  children?: ReactNode
}) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <img
        src="/creative/mark.svg"
        alt="智绘台 VisionDesk"
        className={`shrink-0 shadow-sm ${GLYPH_SIZE[size]}`}
      />
      {children ?? (
        <span className="leading-tight">
          <span className="text-ink block text-[15px] font-bold tracking-tight">智绘台</span>
          <span className="text-muted block text-[9.5px] font-semibold tracking-[1.4px]">
            VISIONDESK
          </span>
        </span>
      )}
    </span>
  )
}
