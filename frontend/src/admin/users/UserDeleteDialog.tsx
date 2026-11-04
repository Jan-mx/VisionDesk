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
  Grid,
  LinearProgress,
  TextField,
  Typography,
} from '@mui/material'

import {
  dataProvider,
  getAdminErrorMessage,
  type AccountDeletionPreview,
  type DeletionOperationOut,
} from '@/admin/dataProvider'
import { formatBytes } from '@/admin/utils'

interface UserDeleteDialogProps {
  open: boolean
  user: { id: string; username: string } | null
  onClose: () => void
  onSuccess: () => void
}

export function UserDeleteDialog({ open, user, onClose, onSuccess }: UserDeleteDialogProps) {
  if (!open || !user) return null
  return (
    <UserDeleteDialogContent
      key={user.id}
      user={user}
      onClose={onClose}
      onSuccess={onSuccess}
    />
  )
}

function UserDeleteDialogContent({
  user,
  onClose,
  onSuccess,
}: {
  user: { id: string; username: string }
  onClose: () => void
  onSuccess: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<AccountDeletionPreview | null>(null)
  const [confirmName, setConfirmName] = useState('')
  const [operation, setOperation] = useState<DeletionOperationOut | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [isRetrying, setIsRetrying] = useState(false)
  const pollTimerRef = useRef<number | null>(null)

  // 挂载时异步获取删除预览
  useEffect(() => {
    let isMounted = true

    dataProvider
      .getDeletionPreview(user.id)
      .then((data) => {
        if (isMounted) {
          setPreview(data)
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
  }, [user.id])

  // 轮询未终结的清理操作
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
        if (updated.status === 'succeeded') {
          onSuccess()
        }
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
  }, [operation, onSuccess])

  const handleConfirmDelete = async () => {
    if (!preview || !preview.preview_token) return
    setIsDeleting(true)
    setError(null)

    try {
      const op = await dataProvider.confirmDeleteUser(
        user.id,
        confirmName.trim(),
        preview.preview_token,
      )
      setOperation(op)
      if (op.status === 'succeeded') {
        onSuccess()
      }
    } catch (err) {
      setError(getAdminErrorMessage(err))
    } finally {
      setIsDeleting(false)
    }
  }

  const handleRetryOperation = async () => {
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
    <Dialog open onClose={isDeleting ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>
        {operation ? '账号清理操作进度' : `永久删除账号：${user.username}`}
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
              正在统计业务关联数据与对象存储文件…
            </Typography>
          </Box>
        )}

        {/* 阶段 1/2：展示预览与输入确认 */}
        {!loading && !operation && preview && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {preview.blocked && (
              <Alert severity="warning" sx={{ mb: 1 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                  当前不可执行删除（存在活动任务或未决状态）
                </Typography>
                <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
                  {preview.blockers.map((b, i) => (
                    <li key={i}>{b.message}</li>
                  ))}
                </ul>
              </Alert>
            )}

            <Typography variant="subtitle2" sx={{ fontWeight: 600, color: '#212335' }}>
              删除范围统计（全量真实口径）：
            </Typography>

            <Grid container spacing={1.5}>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="编辑会话" value={preview.counts.sessions} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="历史记录" value={preview.counts.history} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="工具任务" value={preview.counts.tool_runs} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="Agent 轮次" value={preview.counts.agent_runs} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="素材记录" value={preview.counts.assets} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard
                  label="记录字节"
                  value={formatBytes(preview.counts.asset_record_bytes)}
                />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="存储对象" value={preview.counts.objects} />
              </Grid>
              <Grid size={{ xs: 6, sm: 3 }}>
                <StatCard label="孤儿对象" value={preview.counts.orphan_objects} />
              </Grid>
            </Grid>

            <Alert severity="info" sx={{ mt: 1, fontSize: '12px' }}>
              清理边界说明：在线业务数据与对象文件将被清理；不包含离线备份或已下载副本。
            </Alert>

            {!preview.blocked && (
              <Box sx={{ mt: 2 }}>
                <Typography variant="body2" sx={{ mb: 1, color: '#212335', fontWeight: 600 }}>
                  为防止误操作，请输入目标当前用户名【{user.username}】以确认：
                </Typography>
                <TextField
                  fullWidth
                  size="small"
                  value={confirmName}
                  onChange={(e) => setConfirmName(e.target.value)}
                  placeholder={`请输入 ${user.username}`}
                  autoFocus
                />
              </Box>
            )}
          </Box>
        )}

        {/* 阶段 3：清理操作状态卡 */}
        {operation && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, py: 1 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                操作阶段：{operation.phase}
              </Typography>
              <Chip
                size="small"
                label={
                  operation.status === 'succeeded'
                    ? '已完成'
                    : operation.status === 'failed'
                      ? '清理失败'
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

            <Box sx={{ width: '100%' }}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
                <Typography variant="caption" color="text.secondary">
                  进度：{operation.progress_done} / {operation.progress_total}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  尝试次数：{operation.attempt_count}
                </Typography>
              </Box>
              <LinearProgress
                variant={operation.status === 'processing' ? 'indeterminate' : 'determinate'}
                value={
                  operation.progress_total > 0
                    ? Math.round((operation.progress_done / operation.progress_total) * 100)
                    : 0
                }
                color={operation.status === 'failed' ? 'error' : 'primary'}
                sx={{ height: 8, borderRadius: 4 }}
              />
            </Box>

            {operation.status === 'succeeded' && (
              <Alert severity="success">
                在线业务数据与对象文件已清理，账号墓碑已移除。
              </Alert>
            )}

            {operation.status === 'failed' && (
              <Box sx={{ mt: 1 }}>
                <Alert severity="error" sx={{ mb: 1.5 }}>
                  <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                    清理未完成：{operation.failure_code || '未知错误'}
                  </Typography>
                  <Typography variant="body2">{operation.failure_detail}</Typography>
                </Alert>
                <Button
                  variant="contained"
                  color="primary"
                  onClick={handleRetryOperation}
                  disabled={isRetrying}
                >
                  {isRetrying ? '重试投递中…' : '重试清理操作'}
                </Button>
              </Box>
            )}
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} disabled={isDeleting}>
          {operation?.status === 'succeeded' ? '完成并关闭' : '关闭'}
        </Button>

        {!operation && preview && !preview.blocked && (
          <Button
            variant="contained"
            color="error"
            onClick={handleConfirmDelete}
            disabled={
              isDeleting ||
              confirmName.trim() !== user.username ||
              !preview.preview_token
            }
          >
            {isDeleting ? '正在冻结并投递…' : '确认永久删除'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  )
}

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <Box
      sx={{
        backgroundColor: '#faf9f6',
        border: '1px solid #e6e6ee',
        borderRadius: '8px',
        p: 1.5,
        textAlign: 'center',
      }}
    >
      <Typography variant="caption" sx={{ color: '#6f7182', display: 'block', mb: 0.5 }}>
        {label}
      </Typography>
      <Typography variant="subtitle2" sx={{ fontWeight: 700, color: '#212335' }}>
        {value}
      </Typography>
    </Box>
  )
}
