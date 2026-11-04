import type { AuthProvider } from 'react-admin'

import { authApi } from '@/api/auth'
import { queryClient } from '@/api/queryClient'

export const authProvider: AuthProvider = {
  login: () => Promise.reject(new Error('请使用系统统一登录页面')),
  logout: async () => {
    try {
      await authApi.logout()
    } finally {
      queryClient.clear()
    }
  },
  checkAuth: async () => {
    try {
      const user = await authApi.me()
      if (user.role !== 'admin') {
        return Promise.reject(new Error('无管理员权限'))
      }
      return Promise.resolve()
    } catch {
      return Promise.reject(new Error('未登录'))
    }
  },
  checkError: (error: { status?: number }) => {
    if (error && error.status === 401) {
      return Promise.reject()
    }
    return Promise.resolve()
  },
  getPermissions: async () => {
    try {
      const user = await authApi.me()
      return user.role
    } catch {
      return null
    }
  },
  getIdentity: async () => {
    try {
      const user = await authApi.me()
      return {
        id: user.id,
        fullName: user.username,
      }
    } catch {
      return { id: '', fullName: '' }
    }
  },
}
