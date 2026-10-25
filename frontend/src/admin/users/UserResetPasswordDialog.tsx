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

interface UserResetPasswordDialogProps {
  open: boolean
  user: { id: string; username: string } | null
  isSelf: boolean
  onClose: () => void
  onSuccess: () => void
}

export function UserResetPasswordDialog({
  open,
  user,
  isSelf,
  onClose,
  onSuccess,
}: UserResetPasswordDialogProps) {
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!user || password.length < 6) return
    if (password !== confirmPassword) {
      setError('两次输入的密码不一致')
      return
    }

    setIsPending(true)
    setError(null)

    try {
      await dataProvider.resetPassword(user.id, password)
      setPassword('')
      setConfirmPassword('')
      onSuccess()
      onClose()
    } catch (err) {
      setError(getAdminErrorMessage(err))
    } finally {
      setIsPending(false)
    }
  }

  if (!user) return null

  return (
    <Dialog open={open} onClose={isPending ? undefined : onClose} maxWidth="xs" fullWidth>
      <form onSubmit={handleSubmit}>
        <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>重置密码</DialogTitle>
        <DialogContent dividers>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Typography variant="body2" sx={{ color: '#6f7182', mb: 1 }}>
            目标账号：<strong>{user.username}</strong>
          </Typography>

          {isSelf && (
            <Alert severity="warning" sx={{ mb: 2, fontSize: '12px' }}>
              注意：您正在重置自身管理员密码。提交成功后当前会话将立即失效，需重新登录。
            </Alert>
          )}

          {!isSelf && (
            <Typography variant="caption" sx={{ color: '#8d90a2', display: 'block', mb: 2 }}>
              重置密码后，目标用户所有历史有效会话立即失效，旧令牌无法继续访问。
            </Typography>
          )}

          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="新密码"
              type="password"
              size="small"
              required
              fullWidth
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 6 位字符"
              slotProps={{ htmlInput: { minLength: 6, maxLength: 64 } }}
              autoFocus
            />

            <TextField
              label="确认新密码"
              type="password"
              size="small"
              required
              fullWidth
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="再次输入新密码"
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
            disabled={isPending || password.length < 6 || password !== confirmPassword}
          >
            {isPending ? '正在重置…' : '确认重置'}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
