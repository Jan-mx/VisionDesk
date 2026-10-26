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

import { formatDateTime, formatDuration, toolDisplayName, TOOL_OPTIONS } from '@/admin/utils'
import { ToolRunDetailDialog } from './ToolRunDetailDialog'

const toolRunFilters = [
  <TextInput
    key="q"
    source="q"
    label="搜索（用户名或 UUID）"
    alwaysOn
    size="small"
  />,
  <SelectInput
    key="status"
    source="status"
    label="任务状态"
    alwaysOn
    size="small"
    choices={[
      { id: '__all__', name: '全部' },
      { id: 'queued', name: '排队中 (queued)' },
      { id: 'running', name: '执行中 (running)' },
      { id: 'succeeded', name: '成功 (succeeded)' },
      { id: 'failed', name: '失败 (failed)' },
      { id: 'canceled', name: '已取消 (canceled)' },
    ]}
  />,
  <SelectInput
    key="tool"
    source="tool"
    label="工具"
    size="small"
    choices={[{ id: '__all__', name: '全部' }, ...TOOL_OPTIONS]}
  />,
  <TextInput key="user_id" source="user_id" label="用户 UUID" size="small" />,
  <TextInput
    key="created_from"
    source="created_from"
    label="起始时间 (ISO)"
    size="small"
    placeholder="如 2026-10-01T00:00:00Z"
  />,
  <TextInput
    key="created_to"
    source="created_to"
    label="截止时间 (ISO)"
    size="small"
    placeholder="如 2026-10-31T23:59:59Z"
  />,
]

export function ToolRunList() {
  const [detailRunId, setDetailRunId] = useState<string | null>(null)

  return (
    <Box sx={{ width: '100%', overflowX: 'auto' }}>
      <Box sx={{ mb: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography variant="h6" sx={{ fontWeight: 700, color: '#212335' }}>
          工具任务（只读查询）
        </Typography>
      </Box>

      <List
        filters={toolRunFilters}
        filterDefaultValues={{ status: '__all__', tool: '__all__' }}
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
            minWidth: 880,
            '& .RaDatagrid-table': {
              borderCollapse: 'collapse',
            },
          }}
        >
          <FunctionField
            label="工具"
            render={(record: RaRecord) => (
              <Tooltip title={record.tool} placement="top">
                <Typography variant="body2" sx={{ fontWeight: 600, fontSize: '12.5px' }}>
                  {toolDisplayName(record.tool)}
                </Typography>
              </Tooltip>
            )}
          />

          <FunctionField
            label="状态"
            render={(record: RaRecord) => {
              const colorMap: Record<string, 'success' | 'error' | 'primary' | 'default'> = {
                succeeded: 'success',
                failed: 'error',
                running: 'primary',
                queued: 'default',
                canceled: 'default',
              }
              return (
                <Chip
                  size="small"
                  label={record.status}
                  color={colorMap[record.status] || 'default'}
                  sx={{ fontWeight: 600, fontSize: '11px', borderRadius: '6px' }}
                />
              )
            }}
          />

          <FunctionField
            label="用户"
            render={(record: RaRecord) => (
              <Box>
                <Typography variant="body2" sx={{ fontSize: '12px', fontWeight: 500 }}>
                  {record.username}
                </Typography>
                <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#8d90a2', fontSize: '11px' }}>
                  {record.user_id?.slice(0, 8)}…
                </Typography>
              </Box>
            )}
          />

          <FunctionField
            label="耗时"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#212335' }}>
                {record.duration_ms !== null ? formatDuration(record.duration_ms) : '-'}
              </Typography>
            )}
          />

          <FunctionField
            label="创建时间"
            render={(record: RaRecord) => (
              <Typography variant="body2" sx={{ fontSize: '12px', color: '#6f7182' }}>
                {formatDateTime(record.created_at)}
              </Typography>
            )}
          />

          <FunctionField
            label="会话标识"
            render={(record: RaRecord) => (
              <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#8d90a2' }}>
                {record.session_id ? `${record.session_id.slice(0, 8)}…` : '无会话'}
              </Typography>
            )}
          />

          <FunctionField
            label="操作"
            render={(record: RaRecord) => (
              <Tooltip title="查看任务详情">
                <IconButton
                  size="small"
                  onClick={() => setDetailRunId(String(record.id))}
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

      <ToolRunDetailDialog
        open={Boolean(detailRunId)}
        runId={detailRunId}
        onClose={() => setDetailRunId(null)}
      />
    </Box>
  )
}
