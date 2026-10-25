import { AppBar } from 'react-admin'
import { Link as RouterLink } from 'react-router-dom'
import { Box, Button, Chip, Typography } from '@mui/material'

import { useCurrentUser } from '@/hooks/useAuth'

export function AdminAppBar() {
  const { user } = useCurrentUser()

  return (
    <AppBar
      sx={{
        backgroundColor: '#ffffff',
        color: '#212335',
        boxShadow: 'none',
        borderBottom: '1px solid #e6e6ee',
        px: 2,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flex: 1 }}>
        <img
          src="/creative/mark.svg"
          alt="VisionDesk"
          style={{ width: 28, height: 28, borderRadius: 8 }}
        />
        <Typography
          variant="subtitle1"
          sx={{
            fontWeight: 700,
            fontSize: '15px',
            color: '#212335',
            letterSpacing: '-0.01em',
            display: { xs: 'none', sm: 'block' },
          }}
        >
          VisionDesk · 管理后台
        </Typography>
      </Box>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Button
          component={RouterLink}
          to="/create"
          variant="outlined"
          size="small"
          sx={{
            borderColor: '#e6e6ee',
            color: '#6f7182',
            fontSize: '12px',
            borderRadius: '8px',
            py: 0.5,
            px: 1.5,
            '&:hover': {
              borderColor: '#3448ed',
              color: '#3448ed',
              backgroundColor: '#eeefff',
            },
          }}
        >
          返回工作台
        </Button>

        {user && (
          <Chip
            size="small"
            label={`${user.username} (管理员)`}
            sx={{
              backgroundColor: '#eeefff',
              color: '#3448ed',
              fontWeight: 600,
              fontSize: '11px',
              border: '1px solid #ded8ff',
              borderRadius: '6px',
            }}
          />
        )}
      </Box>
    </AppBar>
  )
}
