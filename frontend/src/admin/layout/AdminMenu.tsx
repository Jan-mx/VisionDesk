import { Menu } from 'react-admin'
import { Box, Typography } from '@mui/material'

function IconWrap({ children }: { children: React.ReactNode }) {
  return (
    <Box
      sx={{
        width: 20,
        height: 20,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        mr: 1.5,
        color: 'currentColor',
      }}
    >
      {children}
    </Box>
  )
}

function UsersIcon() {
  return (
    <IconWrap>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 18, height: 18 }}
      >
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
        <path d="M16 3.13a4 4 0 0 1 0 7.75" />
      </svg>
    </IconWrap>
  )
}

function AssetsIcon() {
  return (
    <IconWrap>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 18, height: 18 }}
      >
        <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
        <circle cx="9" cy="9" r="2" />
        <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" />
      </svg>
    </IconWrap>
  )
}

function ToolRunsIcon() {
  return (
    <IconWrap>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 18, height: 18 }}
      >
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
      </svg>
    </IconWrap>
  )
}

function AgentRunsIcon() {
  return (
    <IconWrap>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 18, height: 18 }}
      >
        <rect width="16" height="16" x="4" y="4" rx="2" />
        <circle cx="9" cy="10" r="1.5" />
        <circle cx="15" cy="10" r="1.5" />
        <path d="M10 14h4" />
        <line x1="12" y1="2" x2="12" y2="4" />
      </svg>
    </IconWrap>
  )
}

function AuditLogsIcon() {
  return (
    <IconWrap>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 18, height: 18 }}
      >
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
        <polyline points="10 9 9 9 8 9" />
      </svg>
    </IconWrap>
  )
}

export function AdminMenu() {
  return (
    <Menu sx={{ mt: 1 }}>
      <Box sx={{ px: 2, py: 1 }}>
        <Typography
          variant="caption"
          sx={{
            color: '#8d90a2',
            fontWeight: 700,
            fontSize: '11px',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}
        >
          核心资源
        </Typography>
      </Box>

      <Menu.Item
        to="/admin/users"
        primaryText="用户维护"
        leftIcon={<UsersIcon />}
        sx={{
          borderRadius: '8px',
          mx: 1,
          mb: 0.5,
          '&.RaMenuItemLink-active': {
            backgroundColor: '#eeefff',
            color: '#3448ed',
            fontWeight: 600,
          },
        }}
      />
      <Menu.Item
        to="/admin/assets"
        primaryText="素材管理"
        leftIcon={<AssetsIcon />}
        sx={{
          borderRadius: '8px',
          mx: 1,
          mb: 0.5,
          '&.RaMenuItemLink-active': {
            backgroundColor: '#eeefff',
            color: '#3448ed',
            fontWeight: 600,
          },
        }}
      />

      <Box sx={{ px: 2, pt: 2, pb: 1 }}>
        <Typography
          variant="caption"
          sx={{
            color: '#8d90a2',
            fontWeight: 700,
            fontSize: '11px',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}
        >
          任务查询
        </Typography>
      </Box>

      <Menu.Item
        to="/admin/tool-runs"
        primaryText="工具任务"
        leftIcon={<ToolRunsIcon />}
        sx={{
          borderRadius: '8px',
          mx: 1,
          mb: 0.5,
          '&.RaMenuItemLink-active': {
            backgroundColor: '#eeefff',
            color: '#3448ed',
            fontWeight: 600,
          },
        }}
      />
      <Menu.Item
        to="/admin/agent-runs"
        primaryText="Agent 轮次"
        leftIcon={<AgentRunsIcon />}
        sx={{
          borderRadius: '8px',
          mx: 1,
          mb: 0.5,
          '&.RaMenuItemLink-active': {
            backgroundColor: '#eeefff',
            color: '#3448ed',
            fontWeight: 600,
          },
        }}
      />

      <Box sx={{ px: 2, pt: 2, pb: 1 }}>
        <Typography
          variant="caption"
          sx={{
            color: '#8d90a2',
            fontWeight: 700,
            fontSize: '11px',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}
        >
          安全审计
        </Typography>
      </Box>

      <Menu.Item
        to="/admin/audit-logs"
        primaryText="操作记录"
        leftIcon={<AuditLogsIcon />}
        sx={{
          borderRadius: '8px',
          mx: 1,
          mb: 0.5,
          '&.RaMenuItemLink-active': {
            backgroundColor: '#eeefff',
            color: '#3448ed',
            fontWeight: 600,
          },
        }}
      />
    </Menu>
  )
}
