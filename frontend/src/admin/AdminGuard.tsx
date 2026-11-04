import { lazy, Suspense } from 'react'
import { Link } from 'react-router-dom'

import { useCurrentUser } from '@/hooks/useAuth'

const AdminApp = lazy(() => import('@/admin/AdminApp'))

export function AdminGuard() {
  const { user, isLoading } = useCurrentUser()

  if (isLoading) {
    return <AdminSkeleton />
  }

  if (user?.role !== 'admin') {
    return <AdminAccessDenied username={user?.username} />
  }

  return (
    <Suspense fallback={<AdminSkeleton />}>
      <AdminApp />
    </Suspense>
  )
}

function AdminSkeleton() {
  return (
    <div className="flex h-screen w-full flex-col bg-canvas animate-pulse">
      {/* 顶栏骨架 */}
      <header className="border-line bg-paper flex h-14 w-full items-center justify-between border-b px-6">
        <div className="flex items-center gap-3">
          <div className="bg-line-strong size-7 rounded-lg" />
          <div className="bg-line h-4 w-36 rounded" />
        </div>
        <div className="bg-line h-8 w-24 rounded-lg" />
      </header>

      {/* 主体左右骨架 */}
      <div className="flex flex-1 overflow-hidden">
        <aside className="border-line bg-paper hidden w-60 border-r p-4 sm:block">
          <div className="flex flex-col gap-2">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="bg-soft h-9 w-full rounded-lg" />
            ))}
          </div>
        </aside>

        <main className="flex-1 p-6">
          <div className="bg-line mb-4 h-6 w-32 rounded" />
          <div className="border-line bg-paper h-80 w-full rounded-xl border p-4" />
        </main>
      </div>
    </div>
  )
}

function AdminAccessDenied({ username }: { username?: string }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-12 text-center">
      <div className="border-line bg-paper shadow-card max-w-md rounded-panel border p-8 animate-pop">
        <div className="bg-peach/80 border-peach-border text-danger mx-auto mb-4 grid size-12 place-items-center rounded-full border text-xl font-bold">
          !
        </div>

        <h2 className="text-ink text-lg font-bold tracking-tight">暂无管理后台访问权限</h2>

        <p className="text-muted mt-2 text-xs leading-relaxed">
          当前登录账号为普通用户（<strong>{username || '当前账号'}</strong>），无权进入后台管理区域。如需管理权限，请联系所有者。
        </p>

        <div className="mt-6">
          <Link
            to="/create"
            className="bg-brand hover:bg-brand-strong rounded-control inline-flex items-center justify-center px-5 py-2.5 text-xs font-semibold text-white shadow-xs transition-colors"
          >
            返回创作工作台
          </Link>
        </div>
      </div>
    </div>
  )
}
