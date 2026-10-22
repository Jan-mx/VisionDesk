import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useCurrentUser } from '@/hooks/useAuth'

export default function RequireAuth() {
  const location = useLocation()
  const { user, isLoading } = useCurrentUser()

  if (isLoading) {
    return <WorkbenchSkeleton />
  }
  return user ? <Outlet /> : <Navigate to="/auth" state={{ from: location }} replace />
}

/** 鉴权未决时的优雅占位骨架屏（杜绝白屏与“加载中…”生硬文本闪烁） */
function WorkbenchSkeleton() {
  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {/* 84px 侧栏占位骨架 */}
      <aside className="border-line bg-paper z-20 flex w-[84px] shrink-0 flex-col items-center border-r px-2 py-5 animate-pulse">
        <div className="bg-line-strong mb-6 size-9 rounded-[11px]" />
        <div className="flex w-full flex-col gap-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="bg-soft h-[58px] w-full rounded-control" />
          ))}
        </div>
      </aside>

      {/* 右侧工作区占位骨架 */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="border-line bg-paper/70 flex h-14 shrink-0 items-center justify-between border-b px-6 backdrop-blur-md animate-pulse">
          <div className="bg-line h-4 w-36 rounded" />
          <div className="bg-line size-7 rounded-full" />
        </header>

        <main className="flex-1 p-8">
          <div className="bg-paper/40 border-line h-64 w-full rounded-panel border animate-pulse" />
        </main>
      </div>
    </div>
  )
}
