import { useRef, useState } from 'react'

import { ACCEPTED_TYPES } from '@/api/assets'
import { checkFile } from '@/hooks/useAssets'

type Props = {
  onFile: (file: File) => void
  disabled?: boolean
  hint?: string
  multiple?: boolean
}

/** 上传区域（复现 creative-v1 方案 A · upload-strip 质感） */
export default function ImageDropzone({ onFile, disabled, hint, multiple }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const accept = (file: File | undefined) => {
    if (!file) return
    const problem = checkFile(file)
    setError(problem)
    if (!problem) onFile(file)
  }

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          if (disabled) return
          const files = Array.from(event.dataTransfer.files)
          if (multiple) files.forEach((file) => accept(file))
          else accept(files[0])
        }}
        className={`bg-paper rounded-card border-1.5 flex flex-wrap items-center justify-between gap-4 p-5 transition-all ${
          dragging
            ? 'border-brand bg-brand-soft ring-4 ring-brand/10'
            : 'border-line hover:border-brand/70 hover:shadow-sm'
        } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
        style={{ borderStyle: 'dashed' }}
      >
        <div className="flex items-center gap-3.5">
          <div className="bg-peach text-[#ad7444] border-peach-border grid size-11 shrink-0 place-items-center rounded-control border">
            <svg viewBox="0 0 24 24" fill="none" className="size-5" aria-hidden>
              <path d="M12 15V3m-5 5 5-5 5 5M4 14v6h16v-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div>
            <h3 className="text-ink text-sm font-bold">
              {disabled ? '上传中…' : multiple ? '拖入多张图片，或点击选择' : '已经有商品图？从这里继续。'}
            </h3>
            <p className="text-muted mt-1 text-xs">
              {hint ?? '支持 JPG、PNG、WebP 图片，单张不超过 20 MB · 自动开启智能修图'}
            </p>
          </div>
        </div>

        <button
          type="button"
          disabled={disabled}
          onClick={() => inputRef.current?.click()}
          className="border-line bg-paper text-ink hover:bg-soft hover:border-line-strong rounded-control shadow-control flex items-center gap-2 border px-4 py-2 text-xs font-semibold transition-all"
        >
          <span>选择图片</span>
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_TYPES.join(',')}
        multiple={multiple}
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          if (multiple) files.forEach((file) => accept(file))
          else accept(files[0])
          event.target.value = ''
        }}
      />

      {error && <p className="text-danger mt-2 text-xs">{error}</p>}
    </div>
  )
}
