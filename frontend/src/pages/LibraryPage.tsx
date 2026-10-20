import { useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { Asset, BatchSkippedItem } from '@/api/assets'
import { ApiError, type ReferenceInfo } from '@/api/client'
import { sessionsApi, type Session, type SessionDeletionPreview } from '@/api/sessions'
import AssetCard from '@/components/AssetCard'
import ConfirmDialog from '@/components/ConfirmDialog'
import { errorMessage } from '@/hooks/useAuth'
import {
  useAssetLibrary,
  useBatchDeleteAssets,
  useDeleteAsset,
  useRenameAsset,
} from '@/hooks/useAssets'
import { useCreateSession, useDeleteSession, usePatchSession, useSessions } from '@/hooks/useSessions'
import { formatDateTime } from '@/lib/format'
import { toast } from '@/stores/toasts'

function formatRefTypes(types: string[]): string {
  const map: Record<string, string> = {
    original: '原图',
    current: '当前图',
    layer: '图层',
    history: '编辑历史',
  }
  return types.map((t) => map[t] || t).join('、')
}

export default function LibraryPage() {
  const navigate = useNavigate()

  // 1. 数据源：默认 limit=50（LIB-FR-002）
  const { data: sessions = [], isPending: sessionsPending } = useSessions(50)
  const { data: groups = [], isPending: libraryPending } = useAssetLibrary(50)

  const createSession = useCreateSession()
  const deleteSessionMutation = useDeleteSession()
  const renameAssetMutation = useRenameAsset()
  const deleteAssetMutation = useDeleteAsset()
  const batchDeleteMutation = useBatchDeleteAssets()

  // 2. 当前选中分组：会话 ID 或 'orphan'（未归入会话伪分组）
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 首次进入选最近会话；无会话则选「未归入会话」，当前选中被删后同规则回退（PRD §6.5）
  const effectiveSelectedId = useMemo(() => {
    if (selectedId === 'orphan') return 'orphan'
    if (selectedId && sessions.some((s) => s.id === selectedId)) return selectedId
    return sessions.length > 0 ? sessions[0].id : 'orphan'
  }, [selectedId, sessions])

  const handleSelectId = (id: string) => {
    setSelectedId(id)
    setSelectedAssetIds(new Set())
    setBatchSkippedItems([])
  }

  // 3. 当前分组对应的数据与素材
  const currentGroup = useMemo(() => {
    if (effectiveSelectedId === 'orphan') {
      return groups.find((g) => g.session_id === null) ?? null
    }
    return groups.find((g) => g.session_id === effectiveSelectedId) ?? null
  }, [groups, effectiveSelectedId])

  const currentSession = useMemo(() => {
    if (effectiveSelectedId === 'orphan') return null
    return sessions.find((s) => s.id === effectiveSelectedId) ?? null
  }, [sessions, effectiveSelectedId])

  const assets = useMemo(() => currentGroup?.assets ?? [], [currentGroup])

  // 4. 多选模式状态：切组时清空（PRD §6.2）
  const [isMultiSelect, setIsMultiSelect] = useState(false)
  const [selectedAssetIds, setSelectedAssetIds] = useState<Set<string>>(new Set())
  const [batchSkippedItems, setBatchSkippedItems] = useState<BatchSkippedItem[]>([])

  const toggleSelectAsset = (id: string) => {
    setSelectedAssetIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAll = () => {
    setSelectedAssetIds(new Set(assets.map((a) => a.id)))
  }

  const unselectAll = () => {
    setSelectedAssetIds(new Set())
  }

  // 5. 行内重命名会话状态
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const isEscapedRef = useRef(false)
  const isSubmittingRef = useRef(false)
  const sessionToRename = usePatchSession(editingSessionId ?? '')

  const startRenameSession = (session: Session) => {
    setEditingSessionId(session.id)
    setEditingTitle(session.title)
    isEscapedRef.current = false
    isSubmittingRef.current = false
  }

  const cancelRenameSession = () => {
    isEscapedRef.current = true
    setEditingSessionId(null)
    setEditingTitle('')
  }

  const commitRenameSession = () => {
    if (isEscapedRef.current || isSubmittingRef.current) return
    isSubmittingRef.current = true

    const normalized = editingTitle.replace(/\s+/g, ' ').trim().slice(0, 80)
    sessionToRename.mutate(
      { title: normalized || undefined },
      {
        onSuccess: () => {
          setEditingSessionId(null)
          setEditingTitle('')
          isSubmittingRef.current = false
          toast('会话已重命名', 'ok')
        },
        onError: (err) => {
          isSubmittingRef.current = false
          toast(errorMessage(err), 'danger')
        },
      },
    )
  }

  // 6. 会话删除流程状态（含删除预览）
  const [deletingSession, setDeletingSession] = useState<Session | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<SessionDeletionPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [sessionDeleteError, setSessionDeleteError] = useState<string | null>(null)
  const sessionDeleteTriggerRef = useRef<HTMLElement | null>(null)
  const sidebarHeadingRef = useRef<HTMLHeadingElement | null>(null)

  const loadDeletionPreview = async (sessionId: string) => {
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

  const openDeleteSession = (session: Session, triggerEl?: HTMLElement) => {
    if (triggerEl) sessionDeleteTriggerRef.current = triggerEl
    setDeletingSession(session)
    setPreviewData(null)
    setPreviewError(null)
    setSessionDeleteError(null)
    void loadDeletionPreview(session.id)
  }

  const confirmDeleteSession = async () => {
    if (!deletingSession || !previewData || previewData.blocked) return
    setSessionDeleteError(null)

    deleteSessionMutation.mutate(
      { id: deletingSession.id, previewToken: previewData.preview_token },
      {
        onSuccess: () => {
          toast('会话及独占素材记录已删除', 'ok')
          const wasSelected = selectedId === deletingSession.id
          setDeletingSession(null)
          setPreviewData(null)
          if (wasSelected) {
            const remaining = sessions.filter((s) => s.id !== deletingSession.id)
            setSelectedId(remaining.length > 0 ? remaining[0].id : 'orphan')
          }
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            if (err.code === 'delete_preview_expired' || err.code === 'delete_preview_changed') {
              setSessionDeleteError(`${err.message}，正在刷新预览…`)
              void loadDeletionPreview(deletingSession.id)
              return
            }
          }
          setSessionDeleteError(errorMessage(err))
        },
      },
    )
  }

  // 7. 素材单删流程状态
  const [deletingAsset, setDeletingAsset] = useState<Asset | null>(null)
  const [assetDeleteError, setAssetDeleteError] = useState<string | null>(null)
  const [assetReferences, setAssetReferences] = useState<ReferenceInfo[] | null>(null)

  const openDeleteAsset = (asset: Asset) => {
    setDeletingAsset(asset)
    setAssetDeleteError(null)
    setAssetReferences(null)
  }

  const confirmDeleteAsset = () => {
    if (!deletingAsset) return
    setAssetDeleteError(null)
    setAssetReferences(null)

    deleteAssetMutation.mutate(deletingAsset.id, {
      onSuccess: () => {
        toast('素材记录已删除', 'ok')
        setDeletingAsset(null)
      },
      onError: (err) => {
        if (err instanceof ApiError) {
          setAssetDeleteError(err.message)
          if (err.references && err.references.length > 0) {
            setAssetReferences(err.references)
          }
        } else {
          setAssetDeleteError(errorMessage(err))
        }
      },
    })
  }

  // 8. 素材重命名弹窗状态
  const [renamingAsset, setRenamingAsset] = useState<Asset | null>(null)
  const [newAssetTitle, setNewAssetTitle] = useState('')
  const [assetRenameError, setAssetRenameError] = useState<string | null>(null)

  const openRenameAsset = (asset: Asset) => {
    setRenamingAsset(asset)
    setNewAssetTitle(asset.title ?? '')
    setAssetRenameError(null)
  }

  const confirmRenameAsset = () => {
    if (!renamingAsset) return
    const trimmed = newAssetTitle.trim()
    if (trimmed.length > 80) {
      setAssetRenameError('素材名称不能超过 80 个字符')
      return
    }
    setAssetRenameError(null)

    renameAssetMutation.mutate(
      { id: renamingAsset.id, title: trimmed || null },
      {
        onSuccess: () => {
          toast('素材已重命名', 'ok')
          setRenamingAsset(null)
        },
        onError: (err) => {
          setAssetRenameError(errorMessage(err))
        },
      },
    )
  }

  // 9. 批量删除弹窗状态
  const [batchConfirmOpen, setBatchConfirmOpen] = useState(false)
  const [batchDeleteError, setBatchDeleteError] = useState<string | null>(null)

  const confirmBatchDelete = () => {
    const assetIds = Array.from(selectedAssetIds)
    if (assetIds.length === 0) return
    setBatchDeleteError(null)

    batchDeleteMutation.mutate(assetIds, {
      onSuccess: (result) => {
        setBatchConfirmOpen(false)
        const deletedCount = result.deleted.length
        const skippedCount = result.skipped.length

        // 移除 deleted 项，保留仍存在的 skipped 项供查看（PRD §6.2）
        const nextSelected = new Set(selectedAssetIds)
        for (const id of result.deleted) {
          nextSelected.delete(id)
        }
        setSelectedAssetIds(nextSelected)

        if (skippedCount > 0) {
          setBatchSkippedItems(result.skipped)
          toast(`已删除 ${deletedCount} 张，跳过 ${skippedCount} 张（详情见提示）`, 'ok')
        } else {
          toast(`已删除 ${deletedCount} 张素材`, 'ok')
          setIsMultiSelect(false)
          setSelectedAssetIds(new Set())
          setBatchSkippedItems([])
        }
      },
      onError: (err) => {
        setBatchDeleteError(errorMessage(err))
      },
    })
  }

  // 10. 卡片点击或「继续编辑」处理
  const handleOpenAssetInEditor = (asset: Asset) => {
    if (effectiveSelectedId && effectiveSelectedId !== 'orphan') {
      navigate(`/editor/${effectiveSelectedId}`)
    } else {
      createSession.mutate(
        { current_asset_id: asset.id },
        { onSuccess: (session) => navigate(`/editor/${session.id}`) },
      )
    }
  }

  const handleContinueGroupEdit = () => {
    if (effectiveSelectedId && effectiveSelectedId !== 'orphan') {
      navigate(`/editor/${effectiveSelectedId}`)
    } else if (assets.length > 0) {
      handleOpenAssetInEditor(assets[0])
    }
  }

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col overflow-x-hidden overflow-y-auto min-[1051px]:flex-row min-[1051px]:overflow-hidden">
      {/* ≤1050px 顶部横向滚动侧栏（chips） */}
      <div className="border-line bg-paper/95 shrink-0 border-b px-4 py-2.5 backdrop-blur-md min-[1051px]:hidden">
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="text-ink flex items-center gap-1.5 font-bold">
            <span className="text-brand">✦</span>
            <span>创意库</span>
          </span>
          <span className="text-faint text-[10px]">最近 50 会话 · 伪分组限 50 张</span>
        </div>
        <div className="scrollbar-slim flex items-center gap-2 overflow-x-auto pb-1">
          {sessions.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => handleSelectId(s.id)}
              className={`shrink-0 rounded-chip px-3 py-1 text-xs transition-colors ${
                effectiveSelectedId === s.id
                  ? 'bg-brand text-white font-medium shadow-xs'
                  : 'bg-soft text-muted hover:text-ink'
              }`}
            >
              {s.title}
            </button>
          ))}
          <button
            type="button"
            onClick={() => handleSelectId('orphan')}
            className={`shrink-0 rounded-chip px-3 py-1 text-xs transition-colors ${
              effectiveSelectedId === 'orphan'
                ? 'bg-brand text-white font-medium shadow-xs'
                : 'bg-soft text-muted hover:text-ink'
            }`}
          >
            未归入会话
          </button>
        </div>
      </div>

      {/* >1050px 左侧会话侧栏（约 288px） */}
      <aside
        className="border-line bg-paper hidden w-[288px] shrink-0 flex-col border-r min-[1051px]:flex"
        aria-label="创意库会话列表"
      >
        {/* 顶部标题与范围常显 */}
        <div className="border-line border-b p-4">
          <div className="flex items-center justify-between">
            <h2
              ref={sidebarHeadingRef}
              tabIndex={-1}
              className="text-ink flex items-center gap-2 text-sm font-bold tracking-tight outline-none"
            >
              <svg viewBox="0 0 24 24" fill="none" className="text-brand size-4.5" aria-hidden>
                <path
                  d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span>创意库</span>
            </h2>
            <span className="bg-lavender text-brand rounded px-1.5 py-0.5 text-[10px] font-bold">
              {sessions.length} 个会话
            </span>
          </div>
          <p className="text-faint mt-1.5 text-[11px] leading-relaxed">
            展示范围：最近 50 个会话；未归入会话最多显示最近 50 张
          </p>
        </div>

        {/* 会话列表 */}
        <div className="scrollbar-slim flex-1 space-y-1 overflow-y-auto p-2">
          {sessionsPending ? (
            <p className="text-faint p-4 text-xs">加载会话中…</p>
          ) : (
            sessions.map((session) => {
              const isSelected = effectiveSelectedId === session.id
              const isEditing = editingSessionId === session.id

              return (
                <div
                  key={session.id}
                  className={`group rounded-control relative flex items-center justify-between p-2 transition-colors ${
                    isSelected
                      ? 'bg-brand-soft text-brand-strong'
                      : 'text-muted hover:bg-soft hover:text-ink'
                  }`}
                >
                  {isEditing ? (
                    <div className="flex w-full items-center gap-1.5">
                      <input
                        type="text"
                        autoFocus
                        value={editingTitle}
                        onChange={(e) => setEditingTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') {
                            e.preventDefault()
                            cancelRenameSession()
                          } else if (e.key === 'Enter') {
                            e.preventDefault()
                            commitRenameSession()
                          }
                        }}
                        onBlur={() => commitRenameSession()}
                        disabled={sessionToRename.isPending}
                        maxLength={80}
                        className="border-brand bg-paper text-ink w-full rounded px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-brand"
                      />
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => handleSelectId(session.id)}
                        className="flex min-w-0 flex-1 flex-col text-left"
                      >
                        <span
                          className={`truncate text-xs font-medium ${
                            isSelected ? 'text-brand-strong font-semibold' : 'text-ink'
                          }`}
                        >
                          {session.title}
                        </span>
                        <span className="text-faint text-[10px] tabular-nums">
                          {formatDateTime(session.updated_at)}
                        </span>
                      </button>

                      {/* Hover / 触屏常显操作按钮（≥44×44 热区） */}
                      <div className="flex shrink-0 items-center opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:opacity-100">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            startRenameSession(session)
                          }}
                          className="hover:text-ink flex min-h-[44px] min-w-[32px] items-center justify-center p-1.5 transition-colors"
                          title="重命名会话"
                          aria-label="重命名会话"
                        >
                          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
                            <path
                              d="m14 5 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"
                              stroke="currentColor"
                              strokeWidth="1.8"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            openDeleteSession(session, e.currentTarget)
                          }}
                          className="hover:text-danger flex min-h-[44px] min-w-[32px] items-center justify-center p-1.5 transition-colors"
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
                      </div>
                    </>
                  )}
                </div>
              )
            })
          )}
        </div>

        {/* 伪分组：未归入会话（即使为空也保留，PRD §6.5） */}
        <div className="border-line border-t p-2">
          <button
            type="button"
            onClick={() => handleSelectId('orphan')}
            className={`rounded-control flex w-full items-center justify-between p-2.5 text-left text-xs transition-colors ${
              effectiveSelectedId === 'orphan'
                ? 'bg-brand-soft text-brand-strong font-semibold'
                : 'text-muted hover:bg-soft hover:text-ink font-medium'
            }`}
          >
            <div className="flex items-center gap-2">
              <span className="text-muted-light">📁</span>
              <span>未归入会话</span>
            </div>
            <span className="text-faint text-[10px] tabular-nums">
              {groups.find((g) => g.session_id === null)?.assets.length ?? 0} 张
            </span>
          </button>
        </div>
      </aside>

      {/* 右侧主素材墙 */}
      <section className="flex min-w-0 flex-1 flex-col overflow-y-auto">
        {/* 分组头部与工具栏 */}
        <div className="border-line bg-paper/70 sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4 backdrop-blur-md">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-ink text-base font-bold tracking-tight">
                {effectiveSelectedId === 'orphan'
                  ? '未归入会话'
                  : currentSession?.title ?? currentGroup?.title ?? '素材分组'}
              </h1>
              <span className="bg-soft text-muted rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums">
                {assets.length} 张
              </span>
            </div>
            {currentSession && (
              <p className="text-faint mt-0.5 text-xs tabular-nums">
                更新时间：{formatDateTime(currentSession.updated_at)}
              </p>
            )}
          </div>

          {/* 工具条：多选、批量删除、继续编辑 */}
          <div className="flex items-center gap-2">
            {isMultiSelect ? (
              <>
                <span className="text-muted text-xs tabular-nums">
                  已选 {selectedAssetIds.size} 项
                </span>
                <button
                  type="button"
                  onClick={selectedAssetIds.size === assets.length ? unselectAll : selectAll}
                  className="border-line text-muted hover:bg-soft hover:text-ink rounded-control border px-2.5 py-1 text-xs font-medium"
                >
                  {selectedAssetIds.size === assets.length ? '取消全选' : '全选'}
                </button>
                <button
                  type="button"
                  onClick={() => setBatchConfirmOpen(true)}
                  disabled={selectedAssetIds.size === 0}
                  className="bg-danger hover:bg-danger/90 rounded-control px-3 py-1 text-xs font-semibold text-white shadow-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                >
                  删除所选
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setIsMultiSelect(false)
                    setSelectedAssetIds(new Set())
                  }}
                  className="border-line text-muted hover:bg-soft hover:text-ink rounded-control border px-2.5 py-1 text-xs font-medium"
                >
                  退出多选
                </button>
              </>
            ) : (
              <>
                {assets.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setIsMultiSelect(true)}
                    className="border-line text-muted hover:bg-soft hover:text-ink rounded-control border px-3 py-1.5 text-xs font-medium transition-colors"
                  >
                    多选
                  </button>
                )}
                {effectiveSelectedId !== 'orphan' && (
                  <button
                    type="button"
                    onClick={handleContinueGroupEdit}
                    className="bg-brand hover:bg-brand-strong rounded-control px-3.5 py-1.5 text-xs font-semibold text-white shadow-xs transition-colors"
                  >
                    在编辑器中打开 →
                  </button>
                )}
              </>
            )}
          </div>
        </div>

        {/* 批删跳过项通知条 */}
        {batchSkippedItems.length > 0 && (
          <div className="bg-peach/60 border-peach-border m-6 mb-0 rounded-card border p-4 text-xs">
            <div className="text-ink mb-1.5 font-bold">部分素材因引用保护已跳过删除：</div>
            <ul className="text-muted list-disc space-y-1 pl-4">
              {batchSkippedItems.map((item) => (
                <li key={item.id}>
                  素材 ID <code className="text-ink font-mono">{item.id.slice(0, 8)}</code>：
                  {item.reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* 素材卡片网格 */}
        <div className="flex-1 p-6">
          {libraryPending || sessionsPending ? (
            <div className="text-faint py-16 text-center text-xs">加载素材中…</div>
          ) : assets.length === 0 ? (
            <div className="border-line bg-paper rounded-card border p-12 text-center">
              <div className="text-faint text-2xl">🖼️</div>
              <h3 className="text-ink mt-2 text-sm font-semibold">当前分组暂无素材</h3>
              <p className="text-muted mt-1 text-xs">
                {selectedId === 'orphan'
                  ? '未归组的素材将在此处汇聚展示'
                  : '可进入编辑器为该会话生成或上传图片'}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
              {assets.map((asset) => (
                <AssetCard
                  key={asset.id}
                  asset={asset}
                  selectable={isMultiSelect}
                  selected={selectedAssetIds.has(asset.id)}
                  onToggleSelect={() => toggleSelectAsset(asset.id)}
                  onSelect={() => handleOpenAssetInEditor(asset)}
                  onRename={() => openRenameAsset(asset)}
                  onDelete={() => openDeleteAsset(asset)}
                />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* 会话删除确认弹窗（LIB-FR-004、015） */}
      <ConfirmDialog
        open={Boolean(deletingSession)}
        title={deletingSession ? `删除会话「${deletingSession.title}」` : '删除会话'}
        confirmText="确认删除"
        cancelText="取消"
        tone="danger"
        isPending={deleteSessionMutation.isPending}
        error={sessionDeleteError}
        confirmDisabled={previewLoading || Boolean(previewError) || previewData?.blocked}
        onConfirm={confirmDeleteSession}
        onClose={() => setDeletingSession(null)}
        triggerRef={sessionDeleteTriggerRef}
        fallbackFocusRef={sidebarHeadingRef}
      >
        <div className="text-muted space-y-2 text-xs leading-relaxed">
          {previewLoading ? (
            <div className="text-faint py-2">正在扫描引用并计算删除范围…</div>
          ) : previewError ? (
            <div className="text-danger flex items-center justify-between py-1">
              <span>获取删除预览失败：{previewError}</span>
              <button
                type="button"
                onClick={() => deletingSession && void loadDeletionPreview(deletingSession.id)}
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

      {/* 单个素材删除确认弹窗（LIB-FR-007） */}
      <ConfirmDialog
        open={Boolean(deletingAsset)}
        title={deletingAsset?.title ? `删除素材「${deletingAsset.title}」` : '删除素材'}
        confirmText="永久删除"
        cancelText="取消"
        tone="danger"
        isPending={deleteAssetMutation.isPending}
        error={assetDeleteError}
        onConfirm={confirmDeleteAsset}
        onClose={() => setDeletingAsset(null)}
      >
        <div className="text-muted space-y-2 text-xs leading-relaxed">
          <p>确定要永久删除该素材吗？该操作将从所有会话图片墙中移除该素材，删除后不可恢复。</p>
          {assetReferences && assetReferences.length > 0 && (
            <div className="bg-peach/50 border-peach-border text-ink rounded-control border p-3">
              <div className="text-danger font-semibold">被以下会话引用保护，无法删除：</div>
              <ul className="mt-1 list-disc pl-4 space-y-1">
                {assetReferences.map((ref) => (
                  <li key={ref.session_id}>
                    会话「<strong>{ref.title}</strong>」：{formatRefTypes(ref.types)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </ConfirmDialog>

      {/* 批量删除素材确认弹窗（LIB-FR-008） */}
      <ConfirmDialog
        open={batchConfirmOpen}
        title={`批量删除素材 (${selectedAssetIds.size} 张)`}
        confirmText="确认删除"
        cancelText="取消"
        tone="danger"
        isPending={batchDeleteMutation.isPending}
        error={batchDeleteError}
        onConfirm={confirmBatchDelete}
        onClose={() => setBatchConfirmOpen(false)}
      >
        <p className="text-muted text-xs leading-relaxed">
          确定要永久删除当前分组选中的 {selectedAssetIds.size}{' '}
          张素材吗？受保护或被引用的素材将被自动跳过并汇报原因。
        </p>
      </ConfirmDialog>

      {/* 素材重命名弹窗（LIB-FR-006） */}
      <ConfirmDialog
        open={Boolean(renamingAsset)}
        title="重命名素材"
        confirmText="保存"
        cancelText="取消"
        tone="brand"
        isPending={renameAssetMutation.isPending}
        error={assetRenameError}
        onConfirm={confirmRenameAsset}
        onClose={() => setRenamingAsset(null)}
      >
        <div className="space-y-1.5">
          <label htmlFor="asset-title-input" className="text-ink text-xs font-medium">
            素材标题（留空则回退元信息展示）
          </label>
          <input
            id="asset-title-input"
            type="text"
            autoFocus
            value={newAssetTitle}
            onChange={(e) => setNewAssetTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                confirmRenameAsset()
              }
            }}
            maxLength={80}
            placeholder="例如：主图-终稿"
            className="border-line bg-paper text-ink w-full rounded-control border px-3 py-1.5 text-xs outline-none focus:border-brand focus:ring-1 focus:ring-brand"
          />
          <div className="text-faint flex justify-between text-[10px]">
            <span>最多 80 个字符</span>
            <span className="tabular-nums">{newAssetTitle.trim().length} / 80</span>
          </div>
        </div>
      </ConfirmDialog>
    </div>
  )
}
