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

interface UserRenameDialogProps {
  open: boolean
  user: { id: string; username: string } | null
  onClose: () => void
  onSuccess: () => void
}

export function UserRenameDialog({ open, user, onClose, onSuccess }: UserRenameDialogProps) {
  if (!open || !user) return null
  return <UserRenameDialogContent key={user.id} user={user} onClose={onClose} onSuccess={onSuccess} />
}

function UserRenameDialogContent({
  user,
  onClose,
  onSuccess,
}: {
  user: { id: string; username: string }
  onClose: () => void
  onSuccess: () => void
}) {
  const [newUsername, setNewUsername] = useState(user.username)
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newUsername.trim() || newUsername.trim() === user.username) return

    setIsPending(true)
    setError(null)

    try {
      await dataProvider.update('users', {
        id: user.id,
        data: { username: newUsername.trim() },
        previousData: user,
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
        <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>修改用户名</DialogTitle>
        <DialogContent dividers>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Typography variant="body2" sx={{ color: '#6f7182', mb: 2 }}>
            当前账号：<strong>{user.username}</strong>
          </Typography>

          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="新用户名"
              size="small"
              required
              fullWidth
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              placeholder="3–32 位字母、数字或下划线"
              slotProps={{ htmlInput: { minLength: 3, maxLength: 32 } }}
              autoFocus
            />
          </Box>
        </DialogContent>

        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={onClose} disabled={isPending}>
            取消
          </Button>
          <Button
            type="submit"
            variant="contained"
            color="primary"
            disabled={
              isPending ||
              !newUsername.trim() ||
              newUsername.trim() === user.username ||
              newUsername.trim().length < 3
            }
          >
            {isPending ? '保存中…' : '保存修改'}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
