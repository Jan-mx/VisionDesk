import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import BrandMark from '@/components/BrandMark'
import LandingHeader from '@/components/landing/LandingHeader'
import { errorMessage, useAuthActions, useCurrentUser } from '@/hooks/useAuth'

type Mode = 'login' | 'register'
type Rules = { minLength?: number; maxLength?: number; pattern?: string; title?: string }

const COPY: Record<Mode, { title: string; submit: string; switchTo: Mode; switchHint: string; sub: string }> = {
  login: {
    title: '欢迎回来。',
    submit: '登录，继续创作',
    switchTo: 'register',
    switchHint: '还没有账号？创建账号',
    sub: '登录智绘台，继续你的下一个好想法。',
  },
  register: {
    title: '开启你的创作之旅',
    submit: '注册并开始创作',
    switchTo: 'login',
    switchHint: '已有账号？去登录',
    sub: '创建账号，把每一个好想法变成作品。',
  },
}

const RULES: Record<Mode, { username: Rules; password: Rules }> = {
  login: { username: {}, password: {} },
  register: {
    username: {
      minLength: 3,
      maxLength: 32,
      pattern: '[\\p{L}\\p{N}_]{3,32}',
      title: '3–32 位字母、数字或下划线',
    },
    password: { minLength: 6, maxLength: 64, title: '6–64 位' },
  },
}

/** 认证登录/注册页（复现 creative-v1 方案 A · 1080px 双栏现代卡片） */
export default function AuthPage() {
  const [params, setParams] = useSearchParams()
  const mode: Mode = params.get('mode') === 'register' ? 'register' : 'login'
  const copy = COPY[mode]

  const navigate = useNavigate()
  const location = useLocation()
  const { user, isLoading } = useCurrentUser()
  const { login, register } = useAuthActions()
  const action = mode === 'login' ? login : register

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isExiting, setIsExiting] = useState(false)

  // 恢复由 RequireAuth 或 URL 参数携带的原本目标路由，默认进入 /create
  const from = (location.state as { from?: Location })?.from?.pathname || params.get('from') || '/create'

  // 未处于提交淡出中且已有登录态时直接跳转
  if (!isLoading && user && !isExiting) return <Navigate to={from} replace />

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (isExiting) return

    action.mutate(
      { username, password },
      {
        onSuccess: () => {
          setIsExiting(true)
          // 220ms 柔和微缩放淡出后平滑进入工作台
          window.setTimeout(() => {
            navigate(from, { replace: true })
          }, 220)
        },
      },
    )
  }

  const switchMode = () => {
    action.reset()
    setParams({ mode: copy.switchTo })
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas">
      <LandingHeader account={null} />

      <main className="flex flex-1 items-center justify-center px-4 py-8 sm:px-6">
        <div
          className={`border-line bg-paper shadow-card grid w-full max-w-[1080px] min-h-[640px] grid-cols-1 overflow-hidden rounded-panel border transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] lg:grid-cols-[1.1fr_1fr] ${
            isExiting ? 'scale-[0.985] opacity-0 pointer-events-none' : 'opacity-100'
          }`}
        >
          {/* 左侧艺术展示栏（浅薰衣草艺术展台 + 倾斜商品卡） */}
          <div className="relative isolate hidden flex-col justify-between overflow-hidden bg-gradient-to-br from-[#eeeaff] to-[#f6f3ff] p-10 lg:flex">
            <div>
              <span className="eyebrow text-[#8c82b4]">A LITTLE IDEA. A LOT OF POSSIBILITIES.</span>
              <h1 className="text-ink mt-4 text-[34px] font-extrabold tracking-tight leading-[1.3]">
                好创意，<br />
                从<span className="text-brand">这里</span>开始。
              </h1>
              <p className="mt-3 text-xs leading-relaxed text-[#7b7396]">
                让灵感落地，让商品发光。<br />
                属于你的 AI 创意工作室。
              </p>
            </div>

            {/* 错位倾斜拍立得海报卡片 */}
            <div className="relative mt-8 h-[300px] w-full">
              <div
                className="shadow-lift pointer-events-none absolute bottom-4 left-6 h-[260px] w-[190px] -rotate-[10deg] overflow-hidden rounded-[12px] border-8 border-white"
                aria-hidden
              >
                <img src="/creative/serum.webp" alt="" className="h-full w-full object-cover" />
              </div>
              <div
                className="shadow-lift pointer-events-none absolute bottom-12 left-48 h-[250px] w-[180px] rotate-[11deg] overflow-hidden rounded-[12px] border-8 border-white"
                aria-hidden
              >
                <img src="/creative/tote.webp" alt="" className="h-full w-full object-cover" />
              </div>

              <span className="pointer-events-none absolute right-8 top-4 text-4xl text-[#d4a0cb] select-none" aria-hidden>
                ✳
              </span>
              <span className="shadow-xs pointer-events-none absolute right-6 bottom-4 rounded-full bg-[#def1ba] px-4 py-2 text-xs font-semibold text-[#557224] -rotate-[7deg]">
                Made with a little imagination.
              </span>
            </div>
          </div>

          {/* 右侧真实表单栏 */}
          <div className="flex flex-col justify-center px-8 py-12 sm:px-14">
            <Link to="/" className="mb-6 inline-block">
              <BrandMark size="sm" />
            </Link>

            <h2 className="text-ink text-[26px] font-bold tracking-tight">{copy.title}</h2>
            <p className="text-muted mt-2 text-xs">{copy.sub}</p>

            <form onSubmit={submit} className="mt-8 space-y-4">
              <Field
                label="用户名"
                value={username}
                onChange={setUsername}
                autoComplete="username"
                placeholder={mode === 'register' ? '3–32 位字母、数字或下划线' : '输入你的用户名'}
                rules={RULES[mode].username}
              />
              <Field
                label="密码"
                type="password"
                value={password}
                onChange={setPassword}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                placeholder={mode === 'register' ? '设置至少 6 位密码' : '输入你的密码'}
                rules={RULES[mode].password}
              />

              {action.isError && (
                <div className="rounded-control bg-danger/10 border-danger/20 border px-3 py-2 text-xs text-danger">
                  {errorMessage(action.error)}
                </div>
              )}

              <button
                type="submit"
                disabled={action.isPending || isExiting}
                className={`rounded-control shadow-sm flex w-full items-center justify-center gap-2 py-3 text-sm font-semibold text-white transition-all duration-200 disabled:opacity-50 ${
                  isExiting
                    ? 'bg-success shadow-success/30 shadow-md scale-[1.01]'
                    : 'bg-brand hover:bg-brand-strong'
                }`}
              >
                <span>
                  {isExiting ? '✓ 验证成功，正在进入…' : action.isPending ? '处理中…' : copy.submit}
                </span>
                {!isExiting && (
                  <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                    <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </button>
            </form>

            <div className="mt-6 text-center text-xs text-muted">
              {copy.switchHint.split('？')[0]}？{' '}
              <button
                type="button"
                onClick={switchMode}
                className="text-brand hover:underline font-semibold"
              >
                {copy.switchHint.split('？')[1]}
              </button>
            </div>

            <p className="text-muted-light mt-8 text-center text-[11px]">
              安全加密登录 · 会话状态由系统持久化保护
            </p>
          </div>
        </div>
      </main>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  autoComplete,
  placeholder,
  rules,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  autoComplete?: string
  placeholder?: string
  rules?: Rules
}) {
  return (
    <label className="block text-left">
      <span className="text-muted mb-1.5 block text-xs font-semibold">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete={autoComplete}
        placeholder={placeholder}
        required
        {...rules}
        className="border-line bg-paper text-ink placeholder:text-muted-light focus:border-brand/70 focus:ring-4 focus:ring-brand/10 rounded-control w-full border px-3.5 py-2.5 text-sm outline-none transition-all"
      />
    </label>
  )
}
