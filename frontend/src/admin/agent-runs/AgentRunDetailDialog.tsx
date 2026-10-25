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
import { formatDateTime } from '@/admin/utils'

interface StepItem {
  tool: string
  status: string
}

interface ArtifactItem {
  asset_id: string
  kind?: string
  url?: string
  missing?: boolean
}

interface AgentRunDetail {
  id: string
  user_id: string
  username: string
  session_id: string
  status: string
  created_at: string
  turn: number
  steps: StepItem[]
  artifacts: ArtifactItem[]
  error: string | null
}

interface AgentRunDetailDialogProps {
  open: boolean
  turnId: string | null
  onClose: () => void
}

export function AgentRunDetailDialog({ open, turnId, onClose }: AgentRunDetailDialogProps) {
  if (!open || !turnId) return null
  return <AgentRunDetailDialogContent key={turnId} turnId={turnId} onClose={onClose} />
}

function AgentRunDetailDialogContent({
  turnId,
  onClose,
}: {
  turnId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<AgentRunDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isMounted = true

    dataProvider
      .getOne('agent-runs', { id: turnId })
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
  }, [turnId])

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>
        Agent 轮次详情：第 {detail?.turn ?? '...'} 轮
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
              正在加载 Agent 轮次记录…
            </Typography>
          </Box>
        )}

        {!loading && detail && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 700, color: '#212335' }}>
                会话内第 {detail.turn} 轮交互
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
              <InfoItem label="轮次 ID" value={detail.id} monospace />
              <InfoItem label="所属用户" value={`${detail.username} (${detail.user_id})`} />
              <InfoItem label="所属会话 ID" value={detail.session_id} monospace />
              <InfoItem label="创建时间" value={formatDateTime(detail.created_at)} />
            </Box>

            {detail.error && (
              <Alert severity="error" sx={{ mt: 1 }}>
                <Typography variant="caption" sx={{ fontWeight: 700, display: 'block' }}>
                  错误摘要（已安全脱敏，剔除提示词与密钥）：
                </Typography>
                <Typography variant="body2" sx={{ mt: 0.5, wordBreak: 'break-all' }}>
                  {detail.error}
                </Typography>
              </Alert>
            )}

            <Divider sx={{ my: 1 }} />

            {/* 执行计划步骤 */}
            <Box>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1, color: '#212335' }}>
                计划执行步骤 ({detail.steps?.length || 0})：
              </Typography>

              {(!detail.steps || detail.steps.length === 0) ? (
                <Typography variant="body2" sx={{ color: '#8d90a2', fontSize: '12px' }}>
                  本轮交互无分解子步骤。
                </Typography>
              ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  {detail.steps.map((st, i) => (
                    <Box
                      key={i}
                      sx={{
                        p: 1.25,
                        borderRadius: '8px',
                        border: '1px solid #e6e6ee',
                        backgroundColor: '#faf9f6',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                      }}
                    >
                      <Typography variant="body2" sx={{ fontWeight: 600, color: '#212335' }}>
                        步骤 {i + 1}：{st.tool}
                      </Typography>
                      <Chip
                        size="small"
                        label={st.status || '未开始'}
                        color={
                          st.status === 'succeeded'
                            ? 'success'
                            : st.status === 'failed'
                              ? 'error'
                              : 'default'
                        }
                        sx={{ fontSize: '11px', height: 20 }}
                      />
                    </Box>
                  ))}
                </Box>
              )}
            </Box>

            <Divider sx={{ my: 1 }} />

            {/* 关联产物 */}
            <Box>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1, color: '#212335' }}>
                关联产物 ({detail.artifacts?.length || 0})：
              </Typography>

              {(!detail.artifacts || detail.artifacts.length === 0) ? (
                <Typography variant="body2" sx={{ color: '#8d90a2', fontSize: '12px' }}>
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
