import { useEffect, useState, useRef } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  LinearProgress,
  Typography,
} from '@mui/material'

import {
  dataProvider,
  getAdminErrorMessage,
  type DeletionOperationOut,
} from '@/admin/dataProvider'
import { formatDateTime } from '@/admin/utils'

interface AuditLogDetail {
  id: string
  actor_id: string
  actor_username: string | null
  target_type: string
  target_id: string
  action: string
  result: string
  request_id: string | null
  summary: Record<string, unknown> | null
  operation_id: string | null
  created_at: string
  operation: DeletionOperationOut | null
}

const ACTION_LABELS: Record<string, string> = {
  user_create: '创建普通用户',
  user_rename: '修改用户名',
  user_disable: '禁用用户账号',
  user_enable: '启用用户账号',
  user_reset_password: '重置用户密码',
  user_delete: '永久删除账号',
  deletion_retry: '重试清理操作',
  asset_rename: '重命名素材',
  asset_delete: '删除素材',
}

interface AuditLogDetailDialogProps {
  open: boolean
  logId: string | null
  onClose: () => void
}

export function AuditLogDetailDialog({ open, logId, onClose }: AuditLogDetailDialogProps) {
  if (!open || !logId) return null
  return <AuditLogDetailDialogContent key={logId} logId={logId} onClose={onClose} />
}

function AuditLogDetailDialogContent({
  logId,
  onClose,
}: {
  logId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<AuditLogDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [operation, setOperation] = useState<DeletionOperationOut | null>(null)
  const [isRetrying, setIsRetrying] = useState(false)
  const pollTimerRef = useRef<number | null>(null)

  useEffect(() => {
    let isMounted = true

    dataProvider
      .getOne('audit-logs', { id: logId })
      .then((res) => {
        if (isMounted) {
          setDetail(res.data)
          setOperation(res.data.operation || null)
          setLoading(false)
        }
      })
      .catch((err) => {
        if (isMounted) {
          setError(getAdminErrorMessage(err))
          setLoading(false)
        }
      })

    return () => {
      isMounted = false
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [logId])

  // 轮询处理中的清理操作
  useEffect(() => {
    if (!operation || operation.status !== 'processing') {
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
      return
    }

    pollTimerRef.current = window.setInterval(async () => {
      try {
        const updated = await dataProvider.getDeletionOperation(operation.id)
        setOperation(updated)
      } catch {
        // 轮询异常不中断
      }
    }, 2000)

    return () => {
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [operation])

  const handleRetry = async () => {
    if (!operation) return
    setIsRetrying(true)
    setError(null)

    try {
      const op = await dataProvider.retryDeletionOperation(operation.id)
      setOperation(op)
    } catch (err) {
      setError(getAdminErrorMessage(err))
    } finally {
      setIsRetrying(false)
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>
        操作审计记录详情
      </DialogTitle>

      <DialogContent dividers>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {loading && (
          <Box sx={{ py: 4, textAlign: 'center' }}>
            <LinearProgress sx={{ mb: 2 }} />
            <Typography variant="body2" color="text.secondary">
              正在加载审计记录及关联清理状态…
            </Typography>
          </Box>
        )}

        {!loading && detail && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 700, color: '#212335' }}>
                {ACTION_LABELS[detail.action] || detail.action}
              </Typography>
              <Chip
                size="small"
                label={
                  detail.result === 'success'
                    ? '成功'
                    : detail.result === 'failed'
                      ? '失败'
                      : '预期拒绝 (rejected)'
                }
                color={
                  detail.result === 'success'
                    ? 'success'
                    : detail.result === 'failed'
                      ? 'error'
                      : 'warning'
                }
                sx={{ fontWeight: 600 }}
              />
            </Box>

            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1.5 }}>
              <InfoItem label="记录 ID" value={detail.id} monospace />
              <InfoItem
                label="操作者"
                value={
                  detail.actor_username
                    ? `${detail.actor_username} (${detail.actor_id})`
                    : `已注销账号 (${detail.actor_id})`
                }
              />
              <InfoItem label="目标类型" value={detail.target_type} />
              <InfoItem label="目标 ID" value={detail.target_id} monospace />
              <InfoItem label="请求标识 (Request ID)" value={detail.request_id || '-'} monospace />
              <InfoItem label="操作时间" value={formatDateTime(detail.created_at)} />
            </Box>

            {detail.summary && (
              <Box sx={{ mt: 1 }}>
                <Typography variant="caption" sx={{ color: '#8d90a2', fontWeight: 600, display: 'block', mb: 0.5 }}>
                  变更摘要（已脱敏）：
                </Typography>
                <Box
                  sx={{
                    p: 1.5,
                    borderRadius: '8px',
                    backgroundColor: '#faf9f6',
                    border: '1px solid #e6e6ee',
                    fontFamily: 'monospace',
                    fontSize: '12px',
                    wordBreak: 'break-all',
                  }}
                >
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                    {JSON.stringify(detail.summary, null, 2)}
                  </pre>
                </Box>
              </Box>
            )}

            {/* 嵌套清理操作状态卡 */}
            {operation && (
              <>
                <Divider sx={{ my: 1 }} />
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                  <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, color: '#212335' }}>
                      关联账号清理操作详情：
                    </Typography>
                    <Chip
                      size="small"
                      label={
                        operation.status === 'succeeded'
                          ? '已完成'
                          : operation.status === 'failed'
                            ? '失败'
                            : '处理中'
                      }
                      color={
                        operation.status === 'succeeded'
                          ? 'success'
                          : operation.status === 'failed'
                            ? 'error'
                            : 'primary'
                      }
                      sx={{ fontWeight: 600 }}
                    />
                  </Box>

                  <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1 }}>
                    <InfoItem label="清理操作 ID" value={operation.id} monospace />
                    <InfoItem label="阶段 (Phase)" value={operation.phase} />
                    <InfoItem
                      label="处理进度"
                      value={`${operation.progress_done} / ${operation.progress_total}`}
                    />
                    <InfoItem label="尝试次数" value={String(operation.attempt_count)} />
                  </Box>

                  <LinearProgress
                    variant={operation.status === 'processing' ? 'indeterminate' : 'determinate'}
                    value={
                      operation.progress_total > 0
                        ? Math.round((operation.progress_done / operation.progress_total) * 100)
                        : 0
                    }
                    color={operation.status === 'failed' ? 'error' : 'primary'}
                    sx={{ height: 6, borderRadius: 3, my: 0.5 }}
                  />

                  {operation.status === 'failed' && (
                    <Box sx={{ mt: 1 }}>
                      <Alert severity="error" sx={{ mb: 1.5 }}>
                        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                          清理未成功：{operation.failure_code || '未知错误'}
                        </Typography>
                        <Typography variant="body2">{operation.failure_detail}</Typography>
                      </Alert>
                      <Button
                        variant="contained"
                        color="primary"
                        onClick={handleRetry}
                        disabled={isRetrying}
                      >
                        {isRetrying ? '正在投递重试…' : '重试此清理操作'}
                      </Button>
                    </Box>
                  )}

                  {operation.status === 'succeeded' && (
                    <Alert severity="success" sx={{ fontSize: '12px' }}>
                      在线业务数据与对象文件已清理完成。
                    </Alert>
                  )}
                </Box>
              </>
            )}
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose}>关闭</Button>
      </DialogActions>
    </Dialog>
  )
}

function InfoItem({
  label,
  value,
  monospace = false,
}: {
  label: string
  value: string
  monospace?: boolean
}) {
  return (
    <Box>
      <Typography variant="caption" sx={{ color: '#8d90a2', display: 'block' }}>
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{
          color: '#212335',
          fontWeight: 500,
          fontSize: '12px',
          fontFamily: monospace ? 'monospace' : 'inherit',
          wordBreak: 'break-all',
        }}
      >
        {value}
      </Typography>
    </Box>
  )
}
