import { useEffect, useState } from 'react'
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

import { dataProvider, getAdminErrorMessage } from '@/admin/dataProvider'
import { formatDateTime, formatDuration, toolDisplayName } from '@/admin/utils'

interface ArtifactItem {
  asset_id: string
  kind?: string
  url?: string
  missing?: boolean
}

interface ToolRunDetail {
  id: string
  user_id: string
  username: string
  session_id: string | null
  tool: string
  status: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  duration_ms: number | null
  artifacts: ArtifactItem[]
  error: string | null
}

interface ToolRunDetailDialogProps {
  open: boolean
  runId: string | null
  onClose: () => void
}

export function ToolRunDetailDialog({ open, runId, onClose }: ToolRunDetailDialogProps) {
  if (!open || !runId) return null
  return <ToolRunDetailDialogContent key={runId} runId={runId} onClose={onClose} />
}

function ToolRunDetailDialogContent({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<ToolRunDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isMounted = true

    dataProvider
      .getOne('tool-runs', { id: runId })
      .then((res) => {
        if (isMounted) {
          setDetail(res.data)
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
    }
  }, [runId])

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>
        工具任务详情：{toolDisplayName(detail?.tool) || '...'}
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
              正在读取持久任务记录…
            </Typography>
          </Box>
        )}

        {!loading && detail && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 700, color: '#212335' }}>
                工具：{toolDisplayName(detail.tool)}
              </Typography>
              <Chip
                size="small"
                label={detail.status}
                color={
                  detail.status === 'succeeded'
                    ? 'success'
                    : detail.status === 'failed'
                      ? 'error'
                      : detail.status === 'running'
                        ? 'primary'
                        : 'default'
                }
                sx={{ fontWeight: 600 }}
              />
            </Box>

            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1.5 }}>
              <InfoItem label="任务 ID" value={detail.id} monospace />
              <InfoItem label="所属用户" value={`${detail.username} (${detail.user_id})`} />
              <InfoItem label="所属会话 ID" value={detail.session_id || '独立生成（无会话）'} monospace />
              <InfoItem
                label="任务耗时"
                value={detail.duration_ms !== null ? formatDuration(detail.duration_ms) : '未结束 / 无耗时'}
              />
              <InfoItem label="创建时间" value={formatDateTime(detail.created_at)} />
              <InfoItem label="开始时间" value={formatDateTime(detail.started_at)} />
              <InfoItem label="结束时间" value={formatDateTime(detail.finished_at)} />
              <InfoItem label="最近心跳" value={formatDateTime(detail.heartbeat_at)} />
            </Box>

            {detail.error && (
              <Alert severity="error" sx={{ mt: 1 }}>
                <Typography variant="caption" sx={{ fontWeight: 700, display: 'block' }}>
                  错误摘要（已安全脱敏，剔除请求头与密钥）：
                </Typography>
                <Typography variant="body2" sx={{ mt: 0.5, wordBreak: 'break-all' }}>
                  {detail.error}
                </Typography>
              </Alert>
            )}

            <Divider sx={{ my: 1 }} />

            <Box>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1.5, color: '#212335' }}>
                关联产物列表 ({detail.artifacts?.length || 0})：
              </Typography>

              {(!detail.artifacts || detail.artifacts.length === 0) ? (
                <Typography variant="body2" sx={{ color: '#8d90a2' }}>
                  暂无生成产物。
                </Typography>
              ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  {detail.artifacts.map((art, idx) => (
                    <Box
                      key={art.asset_id || idx}
                      sx={{
                        p: 1.5,
                        borderRadius: '8px',
                        border: '1px solid #e6e6ee',
                        backgroundColor: '#faf9f6',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                      }}
                    >
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                        {art.url ? (
                          <img
                            src={art.url}
                            alt="产物预览"
                            style={{
                              width: 38,
                              height: 38,
                              borderRadius: 6,
                              objectFit: 'cover',
                              border: '1px solid #e6e6ee',
                            }}
                          />
                        ) : (
                          <Box
                            sx={{
                              width: 38,
                              height: 38,
                              borderRadius: 6,
                              backgroundColor: '#e6e6ee',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              fontSize: '11px',
                              color: '#8d90a2',
                            }}
                          >
                            无图
                          </Box>
                        )}
                        <Box>
                          <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#212335' }}>
                            {art.asset_id}
                          </Typography>
                          {art.kind && (
                            <Typography variant="caption" sx={{ display: 'block', color: '#6f7182' }}>
                              类型: {art.kind}
                            </Typography>
                          )}
                        </Box>
                      </Box>

                      {art.missing ? (
                        <Chip
                          size="small"
                          label="已清理 / 不存在 (missing)"
                          color="warning"
                          sx={{ fontSize: '11px', fontWeight: 600 }}
                        />
                      ) : (
                        <Chip
                          size="small"
                          label="有效产物"
                          color="success"
                          variant="outlined"
                          sx={{ fontSize: '11px', fontWeight: 600 }}
                        />
                      )}
                    </Box>
                  ))}
                </Box>
              )}
            </Box>
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
