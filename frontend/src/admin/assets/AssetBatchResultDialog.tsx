import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'

import type { BatchDeleteOut } from '@/admin/dataProvider'

interface AssetBatchResultDialogProps {
  open: boolean
  result: BatchDeleteOut | null
  onClose: () => void
}

export function AssetBatchResultDialog({ open, result, onClose }: AssetBatchResultDialogProps) {
  if (!open || !result) return null

  const deletedCount = result.deleted.length
  const skippedCount = result.skipped.length

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontWeight: 700, pb: 1 }}>批量删除执行结果</DialogTitle>
      <DialogContent dividers>
        <Box sx={{ display: 'flex', gap: 2, mb: 3 }}>
          <Box
            sx={{
              flex: 1,
              p: 2,
              borderRadius: '8px',
              backgroundColor: '#e3f3e9',
              border: '1px solid #c8e7d2',
              textAlign: 'center',
            }}
          >
            <Typography variant="caption" sx={{ color: '#2d6d4c', fontWeight: 600 }}>
              成功删除
            </Typography>
            <Typography variant="h5" sx={{ fontWeight: 700, color: '#3b8a61', mt: 0.5 }}>
              {deletedCount} 项
            </Typography>
          </Box>

          <Box
            sx={{
              flex: 1,
              p: 2,
              borderRadius: '8px',
              backgroundColor: skippedCount > 0 ? '#ffeadb' : '#f4f3f0',
              border: `1px solid ${skippedCount > 0 ? '#ffd5b8' : '#e6e6ee'}`,
              textAlign: 'center',
            }}
          >
            <Typography variant="caption" sx={{ color: skippedCount > 0 ? '#b83b3b' : '#6f7182', fontWeight: 600 }}>
              跳过 / 保护阻断
            </Typography>
            <Typography
              variant="h5"
              sx={{ fontWeight: 700, color: skippedCount > 0 ? '#e25858' : '#6f7182', mt: 0.5 }}
            >
              {skippedCount} 项
            </Typography>
          </Box>
        </Box>

        {skippedCount > 0 && (
          <Box>
            <Alert severity="warning" sx={{ mb: 2 }}>
              以下素材因保护策略（如正在使用、活动任务中或内部派生）被跳过：
            </Alert>

            <TableContainer component={Paper} sx={{ maxHeight: 320 }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ fontWeight: 600 }}>素材 ID</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>原因代码</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>说明</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {result.skipped.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell sx={{ fontFamily: 'monospace', fontSize: '11px' }}>
                        {item.id}
                      </TableCell>
                      <TableCell>
                        <Chip size="small" label={item.code} color="error" variant="outlined" />
                      </TableCell>
                      <TableCell sx={{ color: '#212335', fontSize: '12px' }}>
                        {item.reason}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button variant="contained" onClick={onClose}>
          确定
        </Button>
      </DialogActions>
    </Dialog>
  )
}
