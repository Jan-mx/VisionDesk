import { Layout, type LayoutProps } from 'react-admin'

import { AdminAppBar } from './AdminAppBar'
import { AdminMenu } from './AdminMenu'

export function AdminLayout(props: LayoutProps) {
  return (
    <Layout
      {...props}
      appBar={AdminAppBar}
      menu={AdminMenu}
      sx={{
        // 整页不横向滚动：appFrame/contentWithSidebar 截断，content 在自身容器内滚动
        '& .RaLayout-appFrame': {
          minWidth: 0,
          maxWidth: '100vw',
          overflowX: 'hidden',
        },
        '& .RaLayout-contentWithSidebar': {
          minWidth: 0,
          overflowX: 'hidden',
        },
        '& .RaLayout-content': {
          backgroundColor: '#faf9f6',
          p: { xs: 1.5, sm: 2, md: 3 },
          minHeight: 'calc(100vh - 56px)',
          minWidth: 0,
          maxWidth: '100vw',
          overflowX: 'auto',
        },
      }}
    />
  )
}
