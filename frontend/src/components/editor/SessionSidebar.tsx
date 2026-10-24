import { useState } from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { sessionsApi, type Session, type SessionDeletionPreview } from '@/api/sessions'
import ConfirmDialog from '@/components/ConfirmDialog'
import AgentConversation from '@/components/editor/AgentConversation'
import MessageComposer from '@/components/editor/MessageComposer'
import Button from '@/components/ui/Button'
import { useSendMessage, useTurns } from '@/hooks/useAgent'
import { errorMessage } from '@/hooks/useAuth'
import { useDeleteSession, useSessions } from '@/hooks/useSessions'
import { formatDateTime } from '@/lib/format'
import { useEditorUi } from '@/stores/editorUi'
import { toast } from '@/stores/toasts'

export default function SessionSidebar({ activeId }: { activeId: string }) {
  // 历史对话默认收起，尽量把纵向空间留给当前对话
  const [historyOpen, setHistoryOpen] = useState(false)

  return (
    <aside className="border-line bg-paper flex w-[284px] shrink-0 flex-col border-r">
      <div className="border-line flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-brand text-sm">✦</span>
          <span className="text-ink text-[13px] font-bold">AI 创意搭档</span>
        </div>
        <div className="flex items-center gap-1.5">
          <Link
            to="/create"
            className="text-muted hover:text-brand hover:bg-soft rounded-control grid size-7 place-items-center text-xs font-semibold"
            title="新建创作会话"
          >
            ＋
          </Link>
          <Button
            active={historyOpen}
            className="px-2 py-1 text-[11px]"
            onClick={() => setHistoryOpen((open) => !open)}
          >
            历史
          </Button>
        </div>
      </div>

      {historyOpen && <SessionList activeId={activeId} />}

      {activeId ? (
        <Conversation sessionId={activeId} />
      ) : (
        <p className="text-faint min-h-0 flex-1 px-4 py-4 text-xs leading-relaxed">
          打开一个历史对话，或回到创作页开始新的一张。
        </p>
      )}
    </aside>
  )
}

function Conversation({ sessionId }: { sessionId: string }) {
  const send = useSendMessage(sessionId)
  const { data: turns = [] } = useTurns(sessionId)
  const planActive = turns.some((turn) => turn.status === 'queued' || turn.status === 'running')
  const selectedLayerId = useEditorUi((s) => s.selectedLayerId)
  const wallSelectedIds = useEditorUi((s) => s.wallSelectedIds)
  const clearWallSelection = useEditorUi((s) => s.clearWallSelection)

  // 选择意图随消息提交；素材归属与可见性由服务端按会话重建校验
  const sendMessage = (text: string) => {
    send.mutate({
      text,
      context: {
        selected_layer_id: selectedLayerId,
        selected_asset_ids: wallSelectedIds.length > 0 ? wallSelectedIds : undefined,
      },
    })
    if (wallSelectedIds.length > 0) clearWallSelection()
  }

  return (
    <>
      <AgentConversation sessionId={sessionId} />
      <MessageComposer
        pending={send.isPending || planActive}
        error={send.isError ? errorMessage(send.error) : null}
        placeholder={
          planActive
            ? '当前计划进行中，确认、取消或等它结束后再发'
            : '说明要怎么改，例如：去背景再水平翻转。回车发送'
        }
        onSend={sendMessage}
      />
    </>
  )
}

function SessionList({ activeId }: { activeId: string }) {
  const navigate = useNavigate()
  const { data: sessions = [], isPending } = useSessions()
  const deleteMutation = useDeleteSession()

  const [deletingSession, setDeletingSession] = useState<Session | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<SessionDeletionPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const loadPreview = async (sessionId: string) => {
    setPreviewLoading(true)
    setPreviewError(null)
    try {
      const data = await sessionsApi.deletionPreview(sessionId)
      setPreviewData(data)
    } catch (err) {
      setPreviewError(errorMessage(err))
    } finally {
      setPreviewLoading(false)
    }
  }

  const openDelete = (session: Session) => {
    setDeletingSession(session)
    setPreviewData(null)
    setPreviewError(null)
    setDeleteError(null)
    void loadPreview(session.id)
  }

  const handleConfirm = () => {
    if (!deletingSession || !previewData || previewData.blocked) return
    setDeleteError(null)

    deleteMutation.mutate(
      { id: deletingSession.id, previewToken: previewData.preview_token },
      {
        onSuccess: () => {
          toast('会话及独占素材已删除', 'ok')
          const wasActive = deletingSession.id === activeId
          setDeletingSession(null)
          if (wasActive) {
            useEditorUi.getState().setPanel('layers')
            useEditorUi.getState().setSelection(null)
            useEditorUi.getState().closeCrop()
            useEditorUi.getState().setCompareOpen(false)
            navigate('/library', { replace: true })
          }
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            if (err.code === 'delete_preview_expired' || err.code === 'delete_preview_changed') {
              setDeleteError(`${err.message}，正在刷新预览…`)
              void loadPreview(deletingSession.id)
              return
            }
          }
          setDeleteError(errorMessage(err))
        },
      },
    )
  }

  if (isPending) {
    return <p className="text-faint border-line border-b px-4 py-3 text-xs">加载中…</p>
  }

  if (sessions.length === 0) {
    return <p className="text-faint border-line border-b px-4 py-3 text-xs">还没有会话</p>
  }

  return (
    <>
      <ul className="border-line scrollbar-slim animate-fade-in max-h-52 shrink-0 space-y-0.5 overflow-y-auto border-b p-2">
        {sessions.map((session) => (
          <li key={session.id} className="group relative flex items-center justify-between">
            <NavLink
              to={`/editor/${session.id}`}
              className={`rounded-chip block min-w-0 flex-1 px-2.5 py-2 transition-colors duration-150 ${
                session.id === activeId
                  ? 'bg-brand-soft text-brand-strong'
                  : 'text-muted hover:bg-soft hover:text-ink'
              }`}
            >
              <span className="block truncate pr-6 text-xs font-medium">{session.title}</span>
              <span className="text-faint block text-[10px] tabular-nums">
                {formatDateTime(session.updated_at)}
              </span>
            </NavLink>

            <button
              type="button"
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                openDelete(session)
              }}
              className="text-muted hover:text-danger absolute right-1 flex min-h-[44px] min-w-[32px] items-center justify-center p-1.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:opacity-100"
              title="删除会话"
              aria-label="删除会话"
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
                <path
                  d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </li>
        ))}
      </ul>

      <ConfirmDialog
        open={Boolean(deletingSession)}
        title={deletingSession ? `删除会话「${deletingSession.title}」` : '删除会话'}
        confirmText="确认删除"
        cancelText="取消"
        tone="danger"
        isPending={deleteMutation.isPending}
        error={deleteError}
        confirmDisabled={previewLoading || Boolean(previewError) || previewData?.blocked}
        onConfirm={handleConfirm}
        onClose={() => setDeletingSession(null)}
      >
        <div className="text-muted space-y-2 text-xs leading-relaxed">
          {previewLoading ? (
            <div className="text-faint py-2">正在扫描引用并计算删除范围…</div>
          ) : previewError ? (
            <div className="text-danger flex items-center justify-between py-1">
              <span>获取删除预览失败：{previewError}</span>
              <button
                type="button"
                onClick={() => deletingSession && void loadPreview(deletingSession.id)}
                className="text-brand hover:underline"
              >
                重试
              </button>
            </div>
          ) : previewData?.blocked ? (
            <div className="bg-peach/50 border-peach-border rounded-control border p-3">
              <div className="text-danger font-semibold">有任务或计划未结束，请先完成或取消：</div>
              <ul className="text-ink mt-1 list-disc pl-4 space-y-0.5">
                {previewData.blockers.map((b, i) => (
                  <li key={i}>{b.message}</li>
                ))}
              </ul>
            </div>
          ) : previewData ? (
            <>
              <div className="bg-soft text-ink rounded-control p-3">
                <p>
                  预计删除该会话独占的{' '}
                  <strong className="text-danger font-bold tabular-nums">
                    {previewData.delete_asset_count}
                  </strong>{' '}
                  张图片。
                </p>
                <p className="text-muted mt-1">
                  保留{' '}
                  <strong className="text-ink font-bold tabular-nums">
                    {previewData.retained_asset_count}
                  </strong>{' '}
                  张关联图片（含共用或本期不清理的派生素材）。
                </p>
              </div>
              <p className="text-faint text-[11px]">
                删除后无法恢复，对话与编辑历史将一并清理。对象文件清理失败可能延后释放空间。
              </p>
            </>
          ) : null}
        </div>
      </ConfirmDialog>
    </>
  )
}
