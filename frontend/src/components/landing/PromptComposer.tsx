import type { RefObject } from 'react'

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function SparkleIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <path
        d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-3.5 text-muted-light" aria-hidden>
      <path d="m12 2 8 4v6q-1 7-8 10-7-3-8-10V6l8-4Zm-4 10 3 3 5-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

/**
 * 核心输入卡片（复现 creative-v1 方案 A · Composer）
 */
export default function PromptComposer({
  value,
  onChange,
  onSubmit,
  onAttach,
  submitLabel,
  placeholder,
  inputRef,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onAttach: () => void
  submitLabel: string
  placeholder: string
  inputRef?: RefObject<HTMLTextAreaElement | null>
}) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit()
      }}
      className="border-line bg-paper shadow-card hover:shadow-hover rounded-card focus-within:border-brand/70 focus-within:ring-4 focus-within:ring-brand/10 relative border p-4.5 text-left transition-all"
    >
      <textarea
        ref={inputRef}
        rows={3}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            onSubmit()
          }
        }}
        placeholder={placeholder}
        aria-label="描述你想要的画面"
        className="text-ink placeholder:text-muted-light w-full resize-none bg-transparent px-1 pt-1 pb-2 text-[14.5px] leading-relaxed outline-none"
      />

      <div className="border-line/60 flex flex-wrap items-center justify-between gap-3 border-t pt-3.5">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onAttach}
            className="border-line bg-paper text-ink hover:bg-soft hover:border-line-strong rounded-control shadow-control flex min-h-[36px] items-center gap-1.5 border px-3 py-1.5 text-xs font-semibold transition-colors"
          >
            <PlusIcon />
            上传商品图
          </button>
        </div>

        <div className="flex items-center gap-3">
          <span className="text-muted-light hidden text-xs sm:inline">Enter 发送 · Shift+Enter 换行</span>
          <button
            type="submit"
            className="bg-brand hover:bg-brand-strong rounded-control flex min-h-[38px] items-center gap-2 px-4.5 py-2 text-sm font-semibold text-white shadow-xs transition-colors hover:shadow-md"
          >
            <SparkleIcon />
            <span>{submitLabel}</span>
            <ArrowIcon />
          </button>
        </div>
      </div>

      <div className="text-muted-light mt-2.5 flex items-center gap-1.5 px-1 text-[11px]">
        <ShieldIcon />
        <span>灵感交给你，剩下的交给 AI</span>
      </div>
    </form>
  )
}
