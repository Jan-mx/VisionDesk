import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  TextField,
  Typography,
} from '@mui/material'

import { dataProvider, getAdminErrorMessage } from '@/admin/dataProvider'

interface AssetRenameDialogProps {
  open: boolean
  asset: { id: string; title: string | null } | null
  onClose: () => void
  onSuccess: () => void
}

export function AssetRenameDialog({ open, asset, onClose, onSuccess }: AssetRenameDialogProps) {
  if (!open || !asset) return null
  return <AssetRenameDialogContent key={asset.id} asset={asset} onClose={onClose} onSuccess={onSuccess} />
}

function AssetRenameDialogContent({
  asset,
  onClose,
  onSuccess,
}: {
  asset: { id: string; title: string | null }
  onClose: () => void
  onSuccess: () => void
}) {
  const [title, setTitle] = useState(asset.title || '')
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    setIsPending(true)
    setError(null)

    const normalized = title.trim() || null

    try {
      await dataProvider.update('assets', {
        id: asset.id,
        data: { title: normalized },
        previousData: asset,
      })
      onSuccess()
      onClose()
    } catch (err) {
      setError(getAdminErrorMessage(err))
    } finally {
      setIsPending(false)
    }
  }

  return (
    <Dialog open onClose={isPending ? undefined : onClose} maxWidth="xs" fullWidth>
      <form onSubmit={handleSubmit}>
        <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>重命名素材</DialogTitle>
        <DialogContent dividers>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Typography variant="caption" sx={{ color: '#6f7182', display: 'block', mb: 2 }}>
            命名规则：最多 80 个字符，前后空格将自动去除；留空或纯空白将保存为未命名。
          </Typography>

          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="素材标题"
              size="small"
              fullWidth
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="请输入素材标题（可留空）"
              slotProps={{ htmlInput: { maxLength: 80 } }}
              autoFocus
            />
          </Box>
        </DialogContent>

        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={onClose} disabled={isPending}>
            取消
          </Button>
          <Button type="submit" variant="contained" color="primary" disabled={isPending}>
            {isPending ? '保存中…' : '保存'}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
