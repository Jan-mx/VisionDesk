import { useState } from 'react'
import {
  Datagrid,
  FunctionField,
  List,
  SelectInput,
  TextField,
  TextInput,
  useRefresh,
  type RaRecord,
} from 'react-admin'
import {
  Box,
  Button,
  Chip,
  IconButton,
  Tooltip,
  Typography,
} from '@mui/material'

import ConfirmDialog from '@/components/ConfirmDialog'
import { useCurrentUser } from '@/hooks/useAuth'
import { dataProvider, getAdminErrorMessage } from '@/admin/dataProvider'
import { formatBytes, formatDateTime } from '@/admin/utils'
import { UserCreateDialog } from './UserCreateDialog'
import { UserDeleteDialog } from './UserDeleteDialog'
import { UserRenameDialog } from './UserRenameDialog'
import { UserResetPasswordDialog } from './UserResetPasswordDialog'

const userFilters = [
  <TextInput key="q" source="q" label="搜索（用户名或 UUID）" alwaysOn size="small" />,
  <SelectInput
    key="status"
    source="status"
    label="账号状态"
    alwaysOn
    size="small"
    choices={[
      { id: 'active', name: '正常 (active)' },
      { id: 'disabled', name: '已禁用 (disabled)' },
      { id: 'deleting', name: '删除中 (deleting)' },
    ]}
  />,
  <TextInput
    key="created_from"
    source="created_from"
    label="注册起始时间 (ISO)"
    size="small"
    placeholder="如 2026-10-01T00:00:00Z"
  />,
  <TextInput
    key="created_to"
    source="created_to"
    label="注册截止时间 (ISO)"
    size="small"
    placeholder="如 2026-10-31T23:59:59Z"
  />,
]

function UserListActions({ onOpenCreate }: { onOpenCreate: () => void }) {
  return (
    <Box sx={{ display: 'flex', gap: 1, py: 1 }}>
      <Button
        variant="contained"
        color="primary"
        size="small"
        onClick={onOpenCreate}
        sx={{ borderRadius: '8px', fontWeight: 600 }}
      >
        + 新建普通账号
      </Button>
    </Box>
  )
}

export function UserList() {
  const refresh = useRefresh()
  const { user: currentAdmin } = useCurrentUser()

  const [createOpen, setCreateOpen] = useState(false)
  const [renameTarget, setRenameTarget] = useState<RaRecord | null>(null)
  const [resetPwdTarget, setResetPwdTarget] = useState<RaRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<RaRecord | null>(null)

  // 启停状态确认弹窗状态
  const [statusTarget, setStatusTarget] = useState<RaRecord | null>(null)
  const [statusPending, setStatusPending] = useState(false)
  const [statusError, setStatusError] = useState<string | null>(null)

  const handleToggleStatus = async () => {
    if (!statusTarget) return
    setStatusPending(true)
    setStatusError(null)

    const nextStatus = statusTarget.status === 'active' ? 'disabled' : 'active'

    try {
      await dataProvider.update('users', {
        id: statusTarget.id,
        data: { status: nextStatus },
        previousData: statusTarget,
      })
      setStatusTarget(null)
      refresh()
    } catch (err) {
      setStatusError(getAdminErrorMessage(err))
    } finally {
      setStatusPending(false)
    }
  }

  return (
    <Box sx={{ width: '100%', overflowX: 'auto' }}>
      <Box sx={{ mb: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography variant="h6" sx={{ fontWeight: 700, color: '#212335' }}>
          用户维护
        </Typography>
      </Box>

      <List
        filters={userFilters}
        actions={<UserListActions onOpenCreate={() => setCreateOpen(true)} />}
        perPage={25}
        sort={{ field: 'created_at', order: 'DESC' }}
        sx={{
          '& .RaList-main': {
            borderRadius: '12px',
            backgroundColor: '#ffffff',
            border: '1px solid #e6e6ee',
            overflow: 'hidden',
          },
        }}
      >
        <Datagrid
          bulkActionButtons={false}
          rowClick={false}
          sx={{
            minWidth: 860,
            '& .RaDatagrid-table': {
              borderCollapse: 'collapse',
            },
          }}
        >
          <TextField source="username" label="用户名" sx={{ fontWeight: 600 }} />

          <FunctionField
            label="角色"
            render={(record: RaRecord) => (
              <Chip
                size="small"
                label={record.role === 'admin' ? '管理员' : '普通用户'}
                sx={{
                  backgroundColor: record.role === 'admin' ? '#eeefff' : '#f4f3f0',
                  color: record.role === 'admin' ? '#3448ed' : '#6f7182',
                  fontWeight: 600,
                  fontSize: '11px',
                  borderRadius: '6px',
                }}
              />
            )}
          />

          <FunctionField
            label="状态"
            render={(record: RaRecord) => {
              const colorMap: Record<string, { bg: string; text: string; label: string }> = {
                active: { bg: '#e3f3e9', text: '#3b8a61', label: '正常' },
                disabled: { bg: '#ffeadb', text: '#e25858', label: '已禁用' },
                deleting: { bg: '#fff2d6', text: '#b87400', label: '删除中' },
              }
              const cfg = colorMap[record.status] || {
                bg: '#f4f3f0',
                text: '#6f7182',
                label: record.status,
              }
              return (
                <Chip
                  size="small"
                  label={cfg.label}
                  sx={{
                    backgroundColor: cfg.bg,
                    color: cfg.text,
                    fontWeight: 600,
                    fontSize: '11px',
                    borderRadius: '6px',
                  }}
                />
              )
            }}
          />

          <FunctionField
            label="注册时间"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#6f7182' }}>
                {formatDateTime(record.created_at)}
              </Typography>
            )}
          />

          <FunctionField
            label="最后登录"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#6f7182' }}>
                {formatDateTime(record.last_login_at)}
              </Typography>
            )}
          />

          <FunctionField
            label="素材统计"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px', fontWeight: 600 }}>
                  {record.asset_count} 个
                </Typography>
                <Typography variant="caption" sx={{ fontSize: '11px', color: '#8d90a2' }}>
                  {formatBytes(record.asset_record_bytes)}
                </Typography>
              </Box>
            )}
          />

          <FunctionField
            label="会话数"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px' }}>
                {record.session_count}
              </Typography>
            )}
          />

          <FunctionField
            label="任务统计"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px' }}>
                  工具：<strong>{record.tool_run_count}</strong>
                </Typography>
                <Typography variant="caption" sx={{ fontSize: '11px', color: '#6f7182' }}>
                  Agent：<strong>{record.agent_run_count}</strong>
                </Typography>
              </Box>
            )}
          />

          <FunctionField
            label="操作"
            render={(record: RaRecord) => {
              const isDeleting = record.status === 'deleting'
              return (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                  <Tooltip title="修改用户名">
                    <span>
                      <IconButton
                        size="small"
                        aria-label="修改用户名"
                        disabled={isDeleting}
                        onClick={() => setRenameTarget(record)}
                        sx={{ color: '#6f7182', '&:hover': { color: '#3448ed' } }}
                      >
                        <svg
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ width: 15, height: 15 }}
                        >
                          <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                        </svg>
                      </IconButton>
                    </span>
                  </Tooltip>

                  <Tooltip title={record.status === 'active' ? '禁用账号' : '启用账号'}>
                    <span>
                      <IconButton
                        size="small"
                        aria-label={record.status === 'active' ? '禁用账号' : '启用账号'}
                        disabled={isDeleting}
                        onClick={() => {
                          setStatusError(null)
                          setStatusTarget(record)
                        }}
                        sx={{
                          color: record.status === 'active' ? '#6f7182' : '#3b8a61',
                          '&:hover': {
                            color: record.status === 'active' ? '#e25858' : '#2d6d4c',
                          },
                        }}
                      >
                        {record.status === 'active' ? (
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            style={{ width: 15, height: 15 }}
                          >
                            <circle cx="12" cy="12" r="10" />
                            <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
                          </svg>
                        ) : (
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            style={{ width: 15, height: 15 }}
                          >
                            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                            <polyline points="22 4 12 14.01 9 11.01" />
                          </svg>
                        )}
                      </IconButton>
                    </span>
                  </Tooltip>

                  <Tooltip title="重置密码">
                    <span>
                      <IconButton
                        size="small"
                        aria-label="重置密码"
                        disabled={isDeleting}
                        onClick={() => setResetPwdTarget(record)}
                        sx={{ color: '#6f7182', '&:hover': { color: '#3448ed' } }}
                      >
                        <svg
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ width: 15, height: 15 }}
                        >
                          <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
                          <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                        </svg>
                      </IconButton>
                    </span>
                  </Tooltip>

                  <Tooltip title={isDeleting ? '查看清理进度与重试' : '永久删除'}>
                    <IconButton
                      size="small"
                      onClick={() => setDeleteTarget(record)}
                      sx={{
                        color: isDeleting ? '#b87400' : '#e25858',
                        '&:hover': { backgroundColor: '#ffeadb' },
                      }}
                    >
                      {isDeleting ? (
                        <svg
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ width: 15, height: 15 }}
                        >
                          <circle cx="12" cy="12" r="10" />
                          <polyline points="12 6 12 12 16 14" />
                        </svg>
                      ) : (
                        <svg
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ width: 15, height: 15 }}
                        >
                          <path d="M3 6h18" />
                          <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
                          <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                        </svg>
                      )}
                    </IconButton>
                  </Tooltip>
                </Box>
              )
            }}
          />
        </Datagrid>
      </List>

      <UserCreateDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSuccess={refresh}
      />

      <UserRenameDialog
        open={Boolean(renameTarget)}
        user={renameTarget ? { id: String(renameTarget.id), username: renameTarget.username } : null}
        onClose={() => setRenameTarget(null)}
        onSuccess={refresh}
      />

      <UserResetPasswordDialog
        open={Boolean(resetPwdTarget)}
        user={resetPwdTarget ? { id: String(resetPwdTarget.id), username: resetPwdTarget.username } : null}
        isSelf={Boolean(resetPwdTarget && currentAdmin && resetPwdTarget.id === currentAdmin.id)}
        onClose={() => setResetPwdTarget(null)}
        onSuccess={refresh}
      />

      <UserDeleteDialog
        open={Boolean(deleteTarget)}
        user={deleteTarget ? { id: String(deleteTarget.id), username: deleteTarget.username } : null}
        onClose={() => setDeleteTarget(null)}
        onSuccess={refresh}
      />

      {/* 启停确认弹窗 */}
      <ConfirmDialog
        open={Boolean(statusTarget)}
        title={statusTarget?.status === 'active' ? '确认禁用账号' : '确认启用账号'}
        description={
          statusTarget?.status === 'active'
            ? `确定要禁用账号【${statusTarget?.username}】吗？禁用后该账号所有已签发会话立即失效，且无法继续提交新操作。`
            : `确定要重新启用账号【${statusTarget?.username}】吗？启用后该账号可正常登录，旧已撤销令牌不会复活。`
        }
        confirmText={statusTarget?.status === 'active' ? '确认禁用' : '确认启用'}
        tone={statusTarget?.status === 'active' ? 'danger' : 'brand'}
        isPending={statusPending}
        error={statusError}
        onConfirm={handleToggleStatus}
        onClose={() => {
          if (!statusPending) {
            setStatusTarget(null)
            setStatusError(null)
          }
        }}
      />
    </Box>
  )
}
