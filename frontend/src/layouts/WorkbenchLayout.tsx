import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import ToastHost from '@/components/ToastHost'
import { useAuthActions, useCurrentUser } from '@/hooks/useAuth'

const NAV_ITEMS = [
  { to: '/create', label: '创作', path: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4' },
  { to: '/editor', label: '编辑', path: 'm14 5 5 5M4 20l5-1L21 7l-5-5L4 14v6Z' },
  { to: '/marketing', label: '物料', path: 'M5 3h14v18H5zM8 7h8M8 11h5m-5 4h8m-8 3h5' },
  { to: '/batch', label: '批量', path: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z' },
]

const TITLES: Record<string, string> = {
  create: '开始创作',
  editor: '图片编辑',
  marketing: '营销物料',
  batch: '批量处理',
  candidates: '候选图挑选',
}

/**
 * 工作台外壳（复现 creative-v1 方案 A）
 * - 左侧 84px 独立图标导航轨
 * - 普通页面 64px 毛玻璃吸顶面包屑导航栏
 * - 编辑器页面隐藏外层面包屑，交由内部专业文档顶栏托管
 */
export default function WorkbenchLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user } = useCurrentUser()
  const { logout } = useAuthActions()

  const isEditorPage = location.pathname.startsWith('/editor')
  const currentSection = location.pathname.split('/')[1] || 'create'
  const currentTitle = TITLES[currentSection] || '工作台'

  const signOut = () =>
    logout.mutate(undefined, { onSuccess: () => navigate('/', { replace: true }) })

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {/* 84px 固定宽度精致图标侧边栏 */}
      <aside
        className="border-line bg-paper z-20 flex w-[84px] shrink-0 flex-col items-center border-r px-2 py-5"
        aria-label="工作台导航"
      >
        <button
          type="button"
          onClick={() => navigate('/')}
          className="mb-6 flex flex-col items-center gap-1 transition-transform hover:scale-105"
          title="返回智绘台首页"
        >
          <img src="/creative/mark.svg" alt="智绘台" className="size-9 rounded-[11px] shadow-sm" />
        </button>

        <nav className="flex w-full flex-1 flex-col gap-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `flex min-h-[58px] w-full flex-col items-center justify-center gap-1 rounded-control text-[11px] transition-colors ${
                  isActive
                    ? 'bg-brand-soft text-brand font-semibold shadow-xs'
                    : 'text-muted hover:bg-soft hover:text-brand font-medium'
                }`
              }
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-5 shrink-0" aria-hidden>
                <path
                  d={item.path}
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {/* 底部用户头像与登出 */}
        <div className="mt-auto flex flex-col items-center gap-3 pt-3">
          <button
            type="button"
            onClick={signOut}
            title={user ? `${user.username} · 点击退出登录` : '退出登录'}
            className="group relative flex flex-col items-center"
          >
            <span className="bg-lavender text-brand border-lavender-border grid size-8 place-items-center rounded-full border text-xs font-bold transition-transform group-hover:scale-110">
              {user?.username.slice(0, 1).toUpperCase() ?? 'U'}
            </span>
            <span className="text-muted group-hover:text-danger mt-1 text-[9.5px]">退出</span>
          </button>
        </div>
      </aside>

      {/* 右侧主工作区 */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* 普通页面 64px 毛玻璃吸顶面包屑顶栏 */}
        {!isEditorPage && (
          <header className="border-line bg-paper/90 sticky top-0 z-10 flex h-16 shrink-0 items-center justify-between border-b px-8 backdrop-blur-md">
            <div className="text-muted flex items-center gap-2.5 text-[13px]">
              <span>我的工作空间</span>
              <svg viewBox="0 0 24 24" fill="none" className="size-3.5 text-faint" aria-hidden>
                <path d="m9 5 7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
              <strong className="text-ink font-semibold">{currentTitle}</strong>
            </div>

            <div className="flex items-center gap-4 text-xs">
              <span className="text-muted hidden md:inline">每一个好想法，都值得实现</span>
              <span className="bg-lavender text-brand border-lavender-border grid size-7 place-items-center rounded-full border text-[11px] font-bold">
                {user?.username.slice(0, 1).toUpperCase() ?? 'U'}
              </span>
            </div>
          </header>
        )}

        {/* 页面内容注入 */}
        <main className="min-w-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>

      <ToastHost />
    </div>
  )
}
