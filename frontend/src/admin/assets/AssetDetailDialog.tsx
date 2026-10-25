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
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'

import { dataProvider, getAdminErrorMessage } from '@/admin/dataProvider'
import { formatBytes, formatDateTime } from '@/admin/utils'

interface ReferenceItem {
  session_id: string
  title: string
  types: string[]
}

interface AssetDetail {
  id: string
  kind: string
  source: string
  title: string | null
  image_format: string
  width: number
  height: number
  size_bytes: number
  has_alpha: boolean
  created_at: string
  user_id: string
  username: string
  library_hidden: boolean
  references_count: Record<string, number>
  can_rename: boolean
  rename_blocker: string | null
  can_delete: boolean
  delete_blocker: string | null
  preview_url: string
  references: ReferenceItem[]
}

interface AssetDetailDialogProps {
  open: boolean
  assetId: string | null
  onClose: () => void
}

export function AssetDetailDialog({ open, assetId, onClose }: AssetDetailDialogProps) {
  if (!open || !assetId) return null
  return <AssetDetailDialogContent key={assetId} assetId={assetId} onClose={onClose} />
}

function AssetDetailDialogContent({
  assetId,
  onClose,
}: {
  assetId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<AssetDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isMounted = true

    dataProvider
      .getOne('assets', { id: assetId })
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
  }, [assetId])

  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>
        素材详情：{detail?.title || '未命名素材'}
      </DialogTitle>

      <DialogContent dividers>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {loading && (
          <Box sx={{ py: 6, textAlign: 'center' }}>
            <LinearProgress sx={{ mb: 2 }} />
            <Typography variant="body2" color="text.secondary">
              正在加载素材元数据与引用关系树…
            </Typography>
          </Box>
        )}

        {!loading && detail && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {/* 上部：缩略图与主要元数据 */}
            <Box
              sx={{
                display: 'flex',
                flexDirection: { xs: 'column', sm: 'row' },
                gap: 3,
                alignItems: { xs: 'center', sm: 'flex-start' },
              }}
            >
              <Box
                sx={{
                  width: { xs: '100%', sm: 220 },
                  height: 220,
                  backgroundColor: '#f4f3f0',
                  borderRadius: '12px',
                  border: '1px solid #e6e6ee',
                  overflow: 'hidden',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <img
                  src={detail.preview_url}
                  alt={detail.title || '素材预览'}
                  style={{ width: '100%', height: '100%', objectFit: 'contain' }}
                />
              </Box>

              <Box sx={{ flex: 1, width: '100%', display: 'flex', flexDirection: 'column', gap: 1 }}>
                <Typography variant="subtitle1" sx={{ fontWeight: 700, color: '#212335' }}>
                  {detail.title || '（未命名素材）'}
                </Typography>
                <Typography variant="caption" sx={{ color: '#8d90a2', wordBreak: 'break-all' }}>
                  ID: {detail.id}
                </Typography>

                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 1 }}>
                  <Chip size="small" label={`类型: ${detail.kind}`} sx={{ fontWeight: 600 }} />
                  <Chip size="small" label={`来源: ${detail.source}`} variant="outlined" />
                  <Chip
                    size="small"
                    label={detail.library_hidden ? '仅隐藏' : '创意库公开'}
                    color={detail.library_hidden ? 'default' : 'primary'}
                  />
                  <Chip size="small" label={`格式: ${detail.image_format.toUpperCase()}`} />
                  {detail.has_alpha && <Chip size="small" label="透明通道 (Alpha)" color="secondary" />}
                </Box>

                <Box sx={{ mt: 2, display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1 }}>
                  <MetaItem label="归属用户" value={`${detail.username} (${detail.user_id})`} />
                  <MetaItem label="分辨率" value={`${detail.width} × ${detail.height}`} />
                  <MetaItem label="文件大小" value={formatBytes(detail.size_bytes)} />
                  <MetaItem label="创建时间" value={formatDateTime(detail.created_at)} />
                </Box>
              </Box>
            </Box>

            {/* 能力阻断说明 */}
            {(!detail.can_rename || !detail.can_delete) && (
              <Alert severity="warning">
                <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                  操作权限提示：
                </Typography>
                {!detail.can_rename && (
                  <Typography variant="body2">
                    • 无法重命名：{detail.rename_blocker === 'asset_readonly' ? '隐藏素材只读' : detail.rename_blocker}
                  </Typography>
                )}
                {!detail.can_delete && (
                  <Typography variant="body2">
                    • 无法删除：{detail.delete_blocker === 'asset_in_use' ? '素材被会话原图、当前图或图层引用' : detail.delete_blocker}
                  </Typography>
                )}
              </Alert>
            )}

            <Divider />

            {/* 下部：完整引用关系树 */}
            <Box>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1.5, color: '#212335' }}>
                引用详情列表（共关联 {detail.references?.length || 0} 个会话）：
              </Typography>

              {(!detail.references || detail.references.length === 0) ? (
                <Typography variant="body2" sx={{ color: '#8d90a2', py: 2 }}>
                  该素材当前未被任何会话引用。
                </Typography>
              ) : (
                <TableContainer component={Paper} sx={{ maxHeight: 280 }}>
                  <Table size="small" stickyHeader>
                    <TableHead>
                      <TableRow>
                        <TableCell sx={{ fontWeight: 600 }}>会话名称</TableCell>
                        <TableCell sx={{ fontWeight: 600 }}>会话 ID</TableCell>
                        <TableCell sx={{ fontWeight: 600 }}>引用类型</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {detail.references.map((ref) => (
                        <TableRow key={ref.session_id}>
                          <TableCell sx={{ fontWeight: 600 }}>{ref.title || '（未命名会话）'}</TableCell>
                          <TableCell sx={{ fontFamily: 'monospace', fontSize: '11px', color: '#6f7182' }}>
                            {ref.session_id}
                          </TableCell>
                          <TableCell>
                            <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                              {ref.types.map((t) => (
                                <Chip
                                  key={t}
                                  size="small"
                                  label={
                                    t === 'original'
                                      ? '原图'
                                      : t === 'current'
                                        ? '当前图'
                                        : t === 'layer'
                                          ? '图层'
                                          : t === 'history'
                                            ? '历史记录'
                                            : t
                                  }
                                  sx={{
                                    fontSize: '11px',
                                    fontWeight: 600,
                                    backgroundColor:
                                      t === 'original' || t === 'current' || t === 'layer'
                                        ? '#ffeadb'
                                        : '#f4f3f0',
                                    color:
                                      t === 'original' || t === 'current' || t === 'layer'
                                        ? '#e25858'
                                        : '#6f7182',
                                  }}
                                />
                              ))}
                            </Box>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
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

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" sx={{ color: '#8d90a2', display: 'block' }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ color: '#212335', fontWeight: 500, fontSize: '12px' }}>
        {value}
      </Typography>
    </Box>
  )
}
