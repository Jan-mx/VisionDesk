import { useState } from 'react'
import {
  Datagrid,
  FunctionField,
  List,
  SelectInput,
  TextInput,
  useListContext,
  useRefresh,
  useUnselectAll,
  type RaRecord,
} from 'react-admin'
import {
  Box,
  Button,
  Chip,
  IconButton,
  Snackbar,
  Tooltip,
  Typography,
} from '@mui/material'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  dataProvider,
  getAdminErrorMessage,
  type BatchDeleteOut,
} from '@/admin/dataProvider'
import { formatBytes, formatDateTime } from '@/admin/utils'
import { AssetBatchResultDialog } from './AssetBatchResultDialog'
import { AssetDetailDialog } from './AssetDetailDialog'
import { AssetRenameDialog } from './AssetRenameDialog'

const assetFilters = [
  <TextInput key="q" source="q" label="搜索（标题或 UUID）" alwaysOn size="small" />,
  <SelectInput
    key="kind"
    source="kind"
    label="素材类型"
    alwaysOn
    size="small"
    choices={[
      { id: 'upload', name: '用户上传 (upload)' },
      { id: 'generated', name: 'AI 生成 (generated)' },
      { id: 'mask', name: '涂抹遮罩 (mask)' },
      { id: 'subject', name: '主体抠图 (subject)' },
      { id: 'background', name: '背景元素 (background)' },
      { id: 'layer', name: '图层素材 (layer)' },
      { id: 'snapshot', name: '快照存档 (snapshot)' },
      { id: 'export', name: '导出产物 (export)' },
      { id: 'wall', name: '画布整墙 (wall)' },
    ]}
  />,
  <SelectInput
    key="source"
    source="source"
    label="素材来源"
    size="small"
    choices={[
      { id: 'upload', name: '上传 (upload)' },
      { id: 'generate', name: '生图 (generate)' },
      { id: 'edit', name: '编辑 (edit)' },
      { id: 'batch', name: '批量 (batch)' },
    ]}
  />,
  <SelectInput
    key="library_hidden"
    source="library_hidden"
    label="隐藏状态"
    size="small"
    choices={[
      { id: 'false', name: '创意库公开' },
      { id: 'true', name: '仅隐藏/内部' },
    ]}
  />,
  <TextInput
    key="created_from"
    source="created_from"
    label="创建起始时间 (ISO)"
    size="small"
    placeholder="如 2026-10-01T00:00:00Z"
  />,
  <TextInput
    key="created_to"
    source="created_to"
    label="创建截止时间 (ISO)"
    size="small"
    placeholder="如 2026-10-31T23:59:59Z"
  />,
]

function AssetBulkActionButtons({
  onBatchDelete,
}: {
  onBatchDelete: (ids: string[]) => void
}) {
  const { selectedIds } = useListContext()
  if (!selectedIds || selectedIds.length === 0) return null

  return (
    <Button
      variant="contained"
      color="error"
      size="small"
      onClick={() => onBatchDelete(selectedIds as string[])}
      sx={{ borderRadius: '8px', fontWeight: 600, mr: 1 }}
    >
      批量删除所选 ({selectedIds.length})
    </Button>
  )
}

export function AssetList() {
  const refresh = useRefresh()
  const unselectAll = useUnselectAll('assets')

  const [detailTargetId, setDetailTargetId] = useState<string | null>(null)
  const [renameTarget, setRenameTarget] = useState<RaRecord | null>(null)

  // 单删状态
  const [deleteTarget, setDeleteTarget] = useState<RaRecord | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null)

  // 批删状态
  const [batchTargetIds, setBatchTargetIds] = useState<string[] | null>(null)
  const [isBatchDeleting, setIsBatchDeleting] = useState(false)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [batchResult, setBatchResult] = useState<BatchDeleteOut | null>(null)

  const handleSingleDelete = async () => {
    if (!deleteTarget) return
    setIsDeleting(true)
    setDeleteError(null)

    try {
      const res = await dataProvider.delete('assets', {
        id: deleteTarget.id,
        previousData: deleteTarget,
      })
      const isHidden = res.data?.status === 'hidden'
      setDeleteNotice(
        isHidden
          ? '该素材存在历史版本引用，已转为隐藏删除（保留对象供撤销重做）。'
          : '素材已成功物理删除，关联对象文件已提交清理。',
      )
      setDeleteTarget(null)
      refresh()
    } catch (err) {
      setDeleteError(getAdminErrorMessage(err))
    } finally {
      setIsDeleting(false)
    }
  }

  const handleConfirmBatchDelete = async () => {
    if (!batchTargetIds || batchTargetIds.length === 0) return
    setIsBatchDeleting(true)
    setBatchError(null)

    try {
      const res = await dataProvider.batchDeleteAssets(batchTargetIds)
      setBatchTargetIds(null)
      setBatchResult(res)
      unselectAll()
      refresh()
    } catch (err) {
      setBatchError(getAdminErrorMessage(err))
    } finally {
      setIsBatchDeleting(false)
    }
  }

  return (
    <Box sx={{ width: '100%', overflowX: 'auto' }}>
      <Box sx={{ mb: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography variant="h6" sx={{ fontWeight: 700, color: '#212335' }}>
          素材管理
        </Typography>
      </Box>

      <List
        filters={assetFilters}
        perPage={25}
        sort={{ field: 'created_at', order: 'DESC' }}
        sx={{
          '& .RaList-main': {
            borderRadius: '12px',
            backgroundColor: '#ffffff',
            border: '1px solid #e6e6ee',
            overflow: 'hidden',
          },
        }}
      >
        <Datagrid
          rowClick={false}
          bulkActionButtons={
            <AssetBulkActionButtons onBatchDelete={(ids) => setBatchTargetIds(ids)} />
          }
          sx={{
            minWidth: 960,
            '& .RaDatagrid-table': {
              borderCollapse: 'collapse',
            },
          }}
        >
          {/* 缩略图列 */}
          <FunctionField
            label="预览"
            render={(record: RaRecord) => (
              <Box
                sx={{
                  width: 44,
                  height: 44,
                  borderRadius: '8px',
                  backgroundColor: '#f4f3f0',
                  border: '1px solid #e6e6ee',
                  overflow: 'hidden',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: 'pointer',
                }}
                onClick={() => setDetailTargetId(String(record.id))}
              >
                <img
                  src={record.preview_url}
                  alt={record.title || ''}
                  style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                />
              </Box>
            )}
          />

          {/* 标题与 ID */}
          <FunctionField
            label="标题与标识"
            render={(record: RaRecord) => (
              <Box sx={{ maxWidth: 180 }}>
                <Typography
                  variant="body2"
                  sx={{
                    fontWeight: 600,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    color: '#212335',
                  }}
                >
                  {record.title || '（未命名素材）'}
                </Typography>
                <Typography
                  variant="caption"
                  sx={{
                    fontFamily: 'monospace',
                    fontSize: '11px',
                    color: '#8d90a2',
                    display: 'block',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {record.id}
                </Typography>
              </Box>
            )}
          />

          {/* 归属用户 */}
          <FunctionField
            label="归属用户"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', fontWeight: 500 }}>
                {record.username}
              </Typography>
            )}
          />

          {/* 类型 / 来源 */}
          <FunctionField
            label="类型 / 来源"
            render={(record: RaRecord) => (
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
                <Chip
                  size="small"
                  label={record.kind}
                  sx={{ fontSize: '11px', fontWeight: 600, height: 20 }}
                />
                <Typography variant="caption" sx={{ fontSize: '10px', color: '#8d90a2' }}>
                  源自 {record.source}
                </Typography>
              </Box>
            )}
          />

          {/* 规格 */}
          <FunctionField
            label="规格与大小"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px' }}>
                  {record.width} × {record.height}
                </Typography>
                <Typography variant="caption" sx={{ fontSize: '11px', color: '#8d90a2' }}>
                  {formatBytes(record.size_bytes)} ({record.image_format.toUpperCase()})
                </Typography>
              </Box>
            )}
          />

          {/* 引用概况 */}
          <FunctionField
            label="引用摘要"
            render={(record: RaRecord) => {
              const refs = record.references_count || {}
              const original = refs.original || 0
              const current = refs.current || 0
              const layer = refs.layer || 0
              const history = refs.history || 0
              const inUse = original + current + layer > 0

              return (
                <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', maxWidth: 140 }}>
                  {original > 0 && (
                    <Chip
                      size="small"
                      label={`原图:${original}`}
                      sx={{ fontSize: '10px', height: 18, backgroundColor: '#ffeadb', color: '#e25858' }}
                    />
                  )}
                  {current > 0 && (
                    <Chip
                      size="small"
                      label={`当前:${current}`}
                      sx={{ fontSize: '10px', height: 18, backgroundColor: '#ffeadb', color: '#e25858' }}
                    />
                  )}
                  {layer > 0 && (
                    <Chip
                      size="small"
                      label={`图层:${layer}`}
                      sx={{ fontSize: '10px', height: 18, backgroundColor: '#ffeadb', color: '#e25858' }}
                    />
                  )}
                  {history > 0 && (
                    <Chip
                      size="small"
                      label={`历史:${history}`}
                      sx={{ fontSize: '10px', height: 18, backgroundColor: '#f4f3f0', color: '#6f7182' }}
                    />
                  )}
                  {!inUse && history === 0 && (
                    <Typography variant="caption" sx={{ color: '#8d90a2', fontSize: '11px' }}>
                      无引用
                    </Typography>
                  )}
                </Box>
              )
            }}
          />

          {/* 隐藏状态 */}
          <FunctionField
            label="可见性"
            render={(record: RaRecord) => (
              <Chip
                size="small"
                label={record.library_hidden ? '仅隐藏' : '创意库公开'}
                sx={{
                  backgroundColor: record.library_hidden ? '#f4f3f0' : '#eeefff',
                  color: record.library_hidden ? '#8d90a2' : '#3448ed',
                  fontWeight: 600,
                  fontSize: '11px',
                  borderRadius: '6px',
                }}
              />
            )}
          />

          {/* 创建时间 */}
          <FunctionField
            label="创建时间"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#6f7182' }}>
                {formatDateTime(record.created_at)}
              </Typography>
            )}
          />

          {/* 操作 */}
          <FunctionField
            label="操作"
            render={(record: RaRecord) => (
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                <Tooltip title="查看完整详情与引用树">
                  <IconButton
                    size="small"
                    onClick={() => setDetailTargetId(String(record.id))}
                    sx={{ color: '#6f7182', '&:hover': { color: '#3448ed' } }}
                  >
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      style={{ width: 15, height: 15 }}
                    >
                      <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
                      <circle cx="12" cy="12" r="3" />
                    </svg>
                  </IconButton>
                </Tooltip>

                <Tooltip title={record.can_rename ? '重命名' : `不可重命名 (${record.rename_blocker})`}>
                  <span>
                    <IconButton
                      size="small"
                      disabled={!record.can_rename}
                      onClick={() => setRenameTarget(record)}
                      sx={{ color: '#6f7182', '&:hover': { color: '#3448ed' } }}
                    >
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        style={{ width: 15, height: 15 }}
                      >
                        <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                      </svg>
                    </IconButton>
                  </span>
                </Tooltip>

                <Tooltip title={record.can_delete ? '删除素材' : `不可删除 (${record.delete_blocker})`}>
                  <span>
                    <IconButton
                      size="small"
                      disabled={!record.can_delete}
                      onClick={() => {
                        setDeleteError(null)
                        setDeleteTarget(record)
                      }}
                      sx={{ color: '#e25858', '&:hover': { backgroundColor: '#ffeadb' } }}
                    >
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        style={{ width: 15, height: 15 }}
                      >
                        <path d="M3 6h18" />
                        <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
                        <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                      </svg>
                    </IconButton>
                  </span>
                </Tooltip>
              </Box>
            )}
          />
        </Datagrid>
      </List>

      <AssetDetailDialog
        open={Boolean(detailTargetId)}
        assetId={detailTargetId}
        onClose={() => setDetailTargetId(null)}
      />

      <AssetRenameDialog
        open={Boolean(renameTarget)}
        asset={renameTarget ? { id: String(renameTarget.id), title: renameTarget.title } : null}
        onClose={() => setRenameTarget(null)}
        onSuccess={refresh}
      />

      <AssetBatchResultDialog
        open={Boolean(batchResult)}
        result={batchResult}
        onClose={() => setBatchResult(null)}
      />

      {/* 单删确认弹窗 */}
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="确认删除素材"
        description={
          deleteTarget
            ? `确定要删除素材【${deleteTarget.title || deleteTarget.id}】吗？若该素材仅被历史版本引用，将转为隐藏删除；若无引用则直接物理清除。`
            : ''
        }
        confirmText="确认删除"
        tone="danger"
        isPending={isDeleting}
        error={deleteError}
        onConfirm={handleSingleDelete}
        onClose={() => {
          if (!isDeleting) {
            setDeleteTarget(null)
            setDeleteError(null)
          }
        }}
      />

      {/* 批删确认弹窗 */}
      <ConfirmDialog
        open={Boolean(batchTargetIds)}
        title="确认批量删除素材"
        description={
          batchTargetIds
            ? `已选择 ${batchTargetIds.length} 项素材。系统将对每项素材执行跨用户归属与保护核验（正在使用或有活动任务的项将被跳过），确定继续吗？`
            : ''
        }
        confirmText="开始批量删除"
        tone="danger"
        isPending={isBatchDeleting}
        error={batchError}
        onConfirm={handleConfirmBatchDelete}
        onClose={() => {
          if (!isBatchDeleting) {
            setBatchTargetIds(null)
            setBatchError(null)
          }
        }}
      />

      {/* 删除成功提示 */}
      <Snackbar
        open={Boolean(deleteNotice)}
        autoHideDuration={4000}
        onClose={() => setDeleteNotice(null)}
        message={deleteNotice}
      />
    </Box>
  )
}
