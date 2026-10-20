import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

export interface ConfirmDialogProps {
  open: boolean
  title: string
  description?: ReactNode
  children?: ReactNode
  confirmText?: string
  cancelText?: string
  tone?: 'danger' | 'brand'
  isPending?: boolean
  error?: string | null
  confirmDisabled?: boolean
  onConfirm: () => void | Promise<void>
  onClose: () => void
  triggerRef?: React.RefObject<HTMLElement | null>
  fallbackFocusRef?: React.RefObject<HTMLElement | null>
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export default function ConfirmDialog({
  open,
  title,
  description,
  children,
  confirmText = '确认',
  cancelText = '取消',
  tone = 'danger',
  isPending = false,
  error = null,
  confirmDisabled = false,
  onConfirm,
  onClose,
  triggerRef,
  fallbackFocusRef,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelBtnRef = useRef<HTMLButtonElement>(null)
  const previousActiveElement = useRef<HTMLElement | null>(null)

  // 记录打开前的聚焦元素并在关闭时回焦
  useEffect(() => {
    if (open) {
      previousActiveElement.current =
        triggerRef?.current || (document.activeElement as HTMLElement | null)

      // 默认聚焦取消按钮
      const frame = requestAnimationFrame(() => {
        cancelBtnRef.current?.focus()
      })

      return () => cancelAnimationFrame(frame)
    } else {
      // 关闭时回焦触发元素，若已被删除则回焦回退容器
      const frame = requestAnimationFrame(() => {
        const trigger = triggerRef?.current || previousActiveElement.current
        if (trigger && document.body.contains(trigger)) {
          trigger.focus()
        } else if (fallbackFocusRef?.current && document.body.contains(fallbackFocusRef.current)) {
          fallbackFocusRef.current.focus()
        }
      })
      return () => cancelAnimationFrame(frame)
    }
  }, [open, triggerRef, fallbackFocusRef])

  // 焦点陷阱与键盘 Esc 取消
  useEffect(() => {
    if (!open) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!isPending) {
          event.preventDefault()
          onClose()
        }
        return
      }

      if (event.key === 'Tab') {
        const dialog = dialogRef.current
        if (!dialog) return

        const focusables = Array.from(
          dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
        ).filter((el) => el.offsetParent !== null || el.getClientRects().length > 0)

        if (focusables.length === 0) {
          event.preventDefault()
          return
        }

        const first = focusables[0]
        const last = focusables[focusables.length - 1]

        if (event.shiftKey) {
          if (document.activeElement === first || !dialog.contains(document.activeElement)) {
            event.preventDefault()
            last.focus()
          }
        } else {
          if (document.activeElement === last || !dialog.contains(document.activeElement)) {
            event.preventDefault()
            first.focus()
          }
        }
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [open, isPending, onClose])

  if (!open) return null

  const handleBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget && !isPending) {
      onClose()
    }
  }

  const dialogContent = (
    <div
      role="presentation"
      onClick={handleBackdropClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4 backdrop-blur-xs animate-fade-in"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="border-line bg-paper rounded-card shadow-panel w-full max-w-md border p-6 text-left animate-pop"
      >
        <h3 id="confirm-dialog-title" className="text-ink text-base font-bold tracking-tight">
          {title}
        </h3>

        {description && (
          <div className="text-muted mt-2 text-xs leading-relaxed">{description}</div>
        )}

        {children && <div className="mt-3">{children}</div>}

        {error && (
          <div className="bg-peach/60 border-peach-border text-danger mt-3 rounded-control border px-3 py-2 text-xs">
            {error}
          </div>
        )}

        <div className="mt-6 flex items-center justify-end gap-2.5">
          <button
            ref={cancelBtnRef}
            type="button"
            onClick={onClose}
            disabled={isPending}
            className="border-line text-muted hover:bg-soft hover:text-ink rounded-control border px-3.5 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40"
          >
            {cancelText}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isPending || confirmDisabled}
            className={`rounded-control px-4 py-1.5 text-xs font-semibold text-white shadow-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              tone === 'danger'
                ? 'bg-danger hover:bg-danger/90 active:scale-[0.98]'
                : 'bg-brand hover:bg-brand-strong active:scale-[0.98]'
            }`}
          >
            {isPending ? '处理中…' : confirmText}
          </button>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialogContent, document.body) : null
}
