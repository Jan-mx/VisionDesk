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

interface UserCreateDialogProps {
  open: boolean
  onClose: () => void
  onSuccess: () => void
}

export function UserCreateDialog({ open, onClose, onSuccess }: UserCreateDialogProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) return

    setIsPending(true)
    setError(null)

    try {
      await dataProvider.create('users', {
        data: {
          username: username.trim(),
          password,
        },
      })
      setUsername('')
      setPassword('')
      onSuccess()
      onClose()
    } catch (err) {
      setError(getAdminErrorMessage(err))
    } finally {
      setIsPending(false)
    }
  }

  return (
    <Dialog open={open} onClose={isPending ? undefined : onClose} maxWidth="xs" fullWidth>
      <form onSubmit={handleSubmit}>
        <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>新建普通用户</DialogTitle>
        <DialogContent dividers>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Typography variant="caption" sx={{ color: '#6f7182', display: 'block', mb: 2 }}>
            创建账号不签发新会话，不覆盖当前管理员 Cookie。初始状态为正常（active）。
          </Typography>

          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="用户名"
              size="small"
              required
              fullWidth
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3–32 位字母、数字或下划线"
              slotProps={{ htmlInput: { minLength: 3, maxLength: 32 } }}
              autoFocus
            />

            <TextField
              label="密码"
              type="password"
              size="small"
              required
              fullWidth
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="设置至少 6 位密码"
              slotProps={{ htmlInput: { minLength: 6, maxLength: 64 } }}
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
            disabled={isPending || !username.trim() || password.length < 6}
          >
            {isPending ? '创建中…' : '立即创建'}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
