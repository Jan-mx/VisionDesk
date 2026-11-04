import { AdminContext, AdminUI, Resource } from 'react-admin'

import { queryClient } from '@/api/queryClient'
import { authProvider } from './authProvider'
import { dataProvider } from './dataProvider'
import { i18nProvider } from './i18n'
import { AdminLayout } from './layout/AdminLayout'
import { adminTheme } from './theme'
import { UserList } from './users/UserList'
import { AssetList } from './assets/AssetList'
import { ToolRunList } from './tool-runs/ToolRunList'
import { AgentRunList } from './agent-runs/AgentRunList'
import { AuditLogList } from './audit-logs/AuditLogList'

export default function AdminApp() {
  return (
    <AdminContext
      basename="/admin"
      dataProvider={dataProvider}
      authProvider={authProvider}
      i18nProvider={i18nProvider}
      theme={adminTheme}
      queryClient={queryClient}
    >
      <AdminUI layout={AdminLayout} requireAuth={false}>
        <Resource name="users" list={UserList} />
        <Resource name="assets" list={AssetList} />
        <Resource name="tool-runs" list={ToolRunList} />
        <Resource name="agent-runs" list={AgentRunList} />
        <Resource name="audit-logs" list={AuditLogList} />
      </AdminUI>
    </AdminContext>
  )
}
