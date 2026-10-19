import { Link } from 'react-router-dom'

import BrandMark from '@/components/BrandMark'

export default function LandingHeader({ account }: { account: string | null }) {
  return (
    <header className="border-line/70 bg-paper/85 sticky top-0 z-40 border-b backdrop-blur-md">
      <div className="mx-auto flex h-20 max-w-[1260px] items-center justify-between px-6 sm:px-8">
        <Link to="/" aria-label="智绘台首页">
          <BrandMark size="md" />
        </Link>

        {/* 顶部主导航 */}
        <nav className="text-muted hidden items-center gap-8 text-[13.5px] font-medium sm:flex">
          <Link to="/create" className="hover:text-brand transition-colors">
            AI 创作
          </Link>
          <Link to="/editor" className="hover:text-brand transition-colors">
            智能修图
          </Link>
          <Link to="/marketing" className="hover:text-brand transition-colors">
            营销物料
          </Link>
          <Link to="/batch" className="hover:text-brand transition-colors">
            批量处理
          </Link>
        </nav>

        {/* 右侧进入行动按钮 */}
        <div className="flex items-center gap-3">
          {account ? (
            <Link
              to="/create"
              className="bg-brand hover:bg-brand-strong rounded-control flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white shadow-xs transition-colors"
            >
              <span>{account} · 进入工作台</span>
              <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </Link>
          ) : (
            <>
              <Link
                to="/auth"
                className="text-muted hover:text-ink hover:bg-soft rounded-control px-3.5 py-2 text-sm font-medium transition-colors"
              >
                登录
              </Link>
              <Link
                to="/create"
                className="bg-brand hover:bg-brand-strong rounded-control flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white shadow-xs transition-colors"
              >
                <span>开始创作</span>
                <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                  <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </Link>
            </>
          )}
        </div>
      </div>
    </header>
  )
}
