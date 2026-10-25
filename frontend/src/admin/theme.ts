import { defaultLightTheme } from 'react-admin'

export const adminTheme = {
  ...defaultLightTheme,
  palette: {
    ...defaultLightTheme.palette,
    mode: 'light' as const,
    primary: {
      main: '#3448ed',
      light: '#eeefff',
      dark: '#2537c8',
      contrastText: '#ffffff',
    },
    secondary: {
      main: '#2537c8',
      light: '#3448ed',
      dark: '#181926',
      contrastText: '#ffffff',
    },
    success: {
      main: '#3b8a61',
      light: '#e3f3e9',
      dark: '#2d6d4c',
      contrastText: '#ffffff',
    },
    error: {
      main: '#e25858',
      light: '#ffeadb',
      dark: '#c43d3d',
      contrastText: '#ffffff',
    },
    background: {
      default: '#faf9f6',
      paper: '#ffffff',
    },
    text: {
      primary: '#212335',
      secondary: '#6f7182',
      disabled: '#8d90a2',
    },
    divider: '#e6e6ee',
  },
  typography: {
    fontFamily:
      "'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    fontSize: 13,
    button: {
      textTransform: 'none' as const,
      fontWeight: 600,
    },
  },
  shape: {
    borderRadius: 12,
  },
  components: {
    ...defaultLightTheme.components,
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          boxShadow: 'none',
          border: '1px solid #e6e6ee',
          borderRadius: 12,
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: '1px solid #e6e6ee',
          boxShadow: '0 2px 8px rgba(33, 35, 53, 0.04)',
        },
      },
    },
    MuiButton: {
      defaultProps: {
        disableElevation: true,
      },
      styleOverrides: {
        root: {
          borderRadius: 12,
          textTransform: 'none',
          fontWeight: 600,
        },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: {
          borderBottom: '1px solid #e6e6ee',
          padding: '12px 16px',
        },
        head: {
          fontWeight: 600,
          color: '#212335',
          backgroundColor: '#faf9f6',
        },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: {
          '&:hover': {
            backgroundColor: '#f4f3f0 !important',
          },
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: '#ffffff',
          color: '#212335',
          boxShadow: 'none',
          borderBottom: '1px solid #e6e6ee',
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          fontWeight: 600,
        },
      },
    },
  },
}
