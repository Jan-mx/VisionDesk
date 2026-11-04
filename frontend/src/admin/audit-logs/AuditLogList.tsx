import { DateFilterInput } from '@/admin/components/DateFilterInput'
import { useState } from 'react'
import {
  Datagrid,
  FunctionField,
  List,
  SelectInput,
  TextInput,
  type RaRecord,
} from 'react-admin'
import {
  Box,
  Chip,
  IconButton,
  Tooltip,
  Typography,
} from '@mui/material'

import { formatDateTime } from '@/admin/utils'
import { AuditLogDetailDialog } from './AuditLogDetailDialog'

const ACTION_LABELS: Record<string, string> = {
  user_create: '创建普通用户',
  user_rename: '修改用户名',
  user_disable: '禁用用户账号',
  user_enable: '启用用户账号',
  user_reset_password: '重置用户密码',
  user_delete: '永久删除账号',
  deletion_retry: '重试清理操作',
  asset_rename: '重命名素材',
  asset_delete: '删除素材',
}

const auditLogFilters = [
  <SelectInput
    key="action"
    source="action"
    label="操作动作"
    alwaysOn
    size="small"
    emptyValue="__all__"
    emptyText="全部"
    choices={[
      { id: 'user_create', name: '创建用户 (user_create)' },
      { id: 'user_rename', name: '修改用户名 (user_rename)' },
      { id: 'user_disable', name: '禁用用户 (user_disable)' },
      { id: 'user_enable', name: '启用用户 (user_enable)' },
      { id: 'user_reset_password', name: '重置密码 (user_reset_password)' },
      { id: 'user_delete', name: '删除用户 (user_delete)' },
      { id: 'deletion_retry', name: '重试清理 (deletion_retry)' },
      { id: 'asset_rename', name: '重命名素材 (asset_rename)' },
      { id: 'asset_delete', name: '删除素材 (asset_delete)' },
    ]}
  />,
  <SelectInput
    key="result"
    source="result"
    label="结果"
    alwaysOn
    size="small"
    emptyValue="__all__"
    emptyText="全部"
    choices={[
      { id: 'success', name: '成功 (success)' },
      { id: 'rejected', name: '预期拒绝 (rejected)' },
      { id: 'failed', name: '失败 (failed)' },
    ]}
  />,
  <TextInput key="actor_id" source="actor_id" label="操作者 UUID" size="small" />,
  <TextInput key="target_id" source="target_id" label="目标 UUID" size="small" />,
  <DateFilterInput
    key="created_from"
    source="created_from"
    label="起始日期"
    size="small"
  />,
  <DateFilterInput
    key="created_to"
    source="created_to"
    label="截止日期"
    size="small"
  />,
]

export function AuditLogList() {
  const [detailLogId, setDetailLogId] = useState<string | null>(null)

  return (
    <Box sx={{ width: '100%', overflowX: 'auto' }}>
      <Box sx={{ mb: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography variant="h6" sx={{ fontWeight: 700, color: '#212335' }}>
          操作审计记录（只读查询）
        </Typography>
      </Box>

      <List
        filterDefaultValues={{ action: '__all__', result: '__all__' }}
        filters={auditLogFilters}
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
            minWidth: 920,
            '& .RaDatagrid-table': {
              borderCollapse: 'collapse',
            },
          }}
        >
          {/* 操作动作 */}
          <FunctionField
            label="操作动作"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontWeight: 600, color: '#212335' }}>
                  {ACTION_LABELS[record.action] || record.action}
                </Typography>
                <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#8d90a2', fontSize: '11px' }}>
                  {record.action}
                </Typography>
              </Box>
            )}
          />

          {/* 执行结果 */}
          <FunctionField
            label="结果"
            render={(record: RaRecord) => (
              <Chip
                size="small"
                label={
                  record.result === 'success'
                    ? '成功'
                    : record.result === 'failed'
                      ? '失败'
                      : '预期拒绝'
                }
                color={
                  record.result === 'success'
                    ? 'success'
                    : record.result === 'failed'
                      ? 'error'
                      : 'warning'
                }
                sx={{ fontWeight: 600, fontSize: '11px', borderRadius: '6px' }}
              />
            )}
          />

          {/* 操作者 */}
          <FunctionField
            label="操作者"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px', fontWeight: 500 }}>
                  {record.actor_username || '已注销账号'}
                </Typography>
                <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#8d90a2', fontSize: '11px' }}>
                  {record.actor_id?.slice(0, 8)}…
                </Typography>
              </Box>
            )}
          />

          {/* 目标 */}
          <FunctionField
            label="目标"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px', fontWeight: 600 }}>
                  {record.target_type === 'user' ? '用户' : record.target_type === 'asset' ? '素材' : record.target_type}
                </Typography>
                <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#8d90a2', fontSize: '11px' }}>
                  {record.target_id?.slice(0, 8)}…
                </Typography>
              </Box>
            )}
          />

          {/* 摘要预览 */}
          <FunctionField
            label="变更摘要"
            render={(record: RaRecord) => {
              const summary = record.summary
              if (!summary || Object.keys(summary).length === 0) {
                return (
                  <Typography variant="caption" sx={{ color: '#8d90a2' }}>
                    -
                  </Typography>
                )
              }
              const preview = Object.entries(summary)
                .map(([k, v]) => `${k}: ${v}`)
                .join('; ')
              return (
                <Typography
                  variant="caption"
                  sx={{
                    color: '#6f7182',
                    maxWidth: 200,
                    display: 'block',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={preview}
                >
                  {preview}
                </Typography>
              )
            }}
          />

          {/* 操作时间 */}
          <FunctionField
            label="操作时间"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#6f7182' }}>
                {formatDateTime(record.created_at)}
              </Typography>
            )}
          />

          {/* 清理操作标记 */}
          <FunctionField
            label="清理操作"
            render={(record: RaRecord) =>
              record.operation_id ? (
                <Chip
                  size="small"
                  label="含清理任务"
                  variant="outlined"
                  color="warning"
                  sx={{ fontSize: '10px', height: 20 }}
                />
              ) : (
                <Typography variant="caption" sx={{ color: '#8d90a2' }}>
                  -
                </Typography>
              )
            }
          />

          {/* 操作 */}
          <FunctionField
            label="操作"
            render={(record: RaRecord) => (
              <Tooltip title="查看记录详情与操作状态">
                <IconButton
                  size="small"
                  onClick={() => setDetailLogId(String(record.id))}
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
                    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                </IconButton>
              </Tooltip>
            )}
          />
        </Datagrid>
      </List>

      <AuditLogDetailDialog
        open={Boolean(detailLogId)}
        logId={detailLogId}
        onClose={() => setDetailLogId(null)}
      />
    </Box>
  )
}
