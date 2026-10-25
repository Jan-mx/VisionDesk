import type { DataProvider } from 'react-admin'

import { api, ApiError } from '@/api/client'

export const ERROR_MESSAGES: Record<string, string> = {
  username_taken: '该用户名已被占用',
  owner_protected: '所有者账号受保护，不可禁用或删除',
  active_tasks: '有任务或计划未结束，请完成或取消后重试',
  delete_preview_expired: '确认凭证已过期，请刷新预览后重试',
  delete_preview_changed: '目标信息或删除范围已变化，请刷新预览后重试',
  invalid_deletion_token: '删除确认凭证无效或不匹配',
  deletion_unavailable: '账号清理功能未开启（系统配置暂未开放）',
  storage_unavailable: '对象存储暂时不可用，请稍后重试',
  asset_readonly: '隐藏素材只读，不可修改',
  account_deleting: '目标账号删除中，素材变更被拒绝',
  unsupported_asset_kind: '该类型素材为内部派生，本期不支持删除',
  asset_in_use: '素材仍被会话原图、当前图或图层引用，请先解除引用',
  reference_state_invalid: '引用数据无法安全解析，需修复后才能删除',
  not_found: '请求的目标记录不存在',
  batch_interrupted: '批量删除中断，已删除项保留，请刷新确认部分结果',
}

export function getAdminErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code && ERROR_MESSAGES[error.code]) {
      return ERROR_MESSAGES[error.code]
    }
    return error.message || '请求失败'
  }
  return error instanceof Error ? error.message : '操作失败，请稍后重试'
}

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface DeletionCounts {
  sessions: number
  history: number
  tool_runs: number
  agent_runs: number
  assets: number
  asset_record_bytes: number
  objects: number
  orphan_objects: number
}

export interface DeletionBlocker {
  code: string
  message: string
}

export interface AccountDeletionPreview {
  user_id: string
  username: string
  counts: DeletionCounts
  blocked: boolean
  blockers: DeletionBlocker[]
  preview_token: string | null
  expires_at: string | null
}

export interface DeletionOperationOut {
  id: string
  target_user_id: string
  status: 'processing' | 'failed' | 'succeeded'
  phase: string
  progress_done: number
  progress_total: number
  attempt_count: number
  created_at: string
  updated_at: string
  finished_at: string | null
  failure_code: string | null
  failure_detail: string | null
}

export interface BatchSkippedItem {
  id: string
  code: string
  reason: string
}

export interface BatchDeleteOut {
  deleted: string[]
  skipped: BatchSkippedItem[]
}

const RESOURCE_SORT_WHITELIST: Record<string, string[]> = {
  users: ['created_at', 'username', 'status'],
  assets: ['created_at', 'size_bytes', 'title'],
  'tool-runs': ['created_at', 'status', 'tool'],
  'agent-runs': ['created_at', 'status'],
  'audit-logs': ['created_at', 'action', 'result'],
}

const RESOURCE_FILTER_WHITELIST: Record<string, string[]> = {
  users: ['q', 'status', 'created_from', 'created_to'],
  assets: ['user_id', 'q', 'kind', 'source', 'library_hidden', 'created_from', 'created_to'],
  'tool-runs': ['user_id', 'status', 'tool', 'created_from', 'created_to'],
  'agent-runs': ['user_id', 'status', 'session_id', 'created_from', 'created_to'],
  'audit-logs': ['actor_id', 'target_id', 'action', 'result', 'created_from', 'created_to'],
}

function normalizeIsoTime(val: unknown): string | undefined {
  if (!val) return undefined
  if (typeof val === 'string' && val.trim()) {
    try {
      const d = new Date(val)
      if (!isNaN(d.getTime())) {
        return d.toISOString()
      }
    } catch {
      return undefined
    }
  }
  return undefined
}

function buildQueryParams(
  resource: string,
  params: {
    pagination?: { page: number; perPage: number }
    sort?: { field: string; order: string }
    filter?: Record<string, unknown>
  },
): string {
  const query = new URLSearchParams()
  const page = params.pagination?.page ?? 1
  const pageSize = params.pagination?.perPage ?? 25
  query.set('page', String(page))
  query.set('page_size', String(pageSize))

  const allowedSorts = RESOURCE_SORT_WHITELIST[resource] || ['created_at']
  const sortField = params.sort?.field && allowedSorts.includes(params.sort.field)
    ? params.sort.field
    : 'created_at'
  const sortOrder = (params.sort?.order || 'DESC').toLowerCase() === 'asc' ? 'asc' : 'desc'
  query.set('sort', sortField)
  query.set('order', sortOrder)

  const allowedFilters = RESOURCE_FILTER_WHITELIST[resource] || []
  const filterObj = params.filter || {}
  for (const key of allowedFilters) {
    const raw = filterObj[key]
    if (raw === undefined || raw === null || raw === '') continue

    if (key === 'created_from' || key === 'created_to') {
      const iso = normalizeIsoTime(raw)
      if (iso) query.set(key, iso)
    } else if (key === 'library_hidden') {
      if (raw === true || raw === 'true') query.set(key, 'true')
      else if (raw === false || raw === 'false') query.set(key, 'false')
    } else {
      query.set(key, String(raw).trim())
    }
  }

  return query.toString()
}

export interface CustomAdminDataProvider extends DataProvider {
  resetPassword: (userId: string, password: string) => Promise<void>
  getDeletionPreview: (userId: string) => Promise<AccountDeletionPreview>
  confirmDeleteUser: (
    userId: string,
    confirmUsername: string,
    previewToken: string,
  ) => Promise<DeletionOperationOut>
  getDeletionOperation: (operationId: string) => Promise<DeletionOperationOut>
  retryDeletionOperation: (operationId: string) => Promise<DeletionOperationOut>
  batchDeleteAssets: (assetIds: string[]) => Promise<BatchDeleteOut>
}

export const dataProvider: CustomAdminDataProvider = {
  getList: async (resource, params) => {
    const qs = buildQueryParams(resource, params)
    const res = await api.get<PageResponse<any>>(`/admin/${resource}?${qs}`)
    return {
      data: res.items,
      total: res.total,
    }
  },

  getOne: async (resource, params) => {
    const res = await api.get<any>(`/admin/${resource}/${params.id}`)
    return {
      data: res,
    }
  },

  getMany: async (resource, params) => {
    // 聚合多个 ID：逐项获取或通过 getList 兜底
    const items = await Promise.all(
      params.ids.map(async (id) => {
        try {
          const res = await api.get<any>(`/admin/${resource}/${id}`)
          return res
        } catch {
          return null
        }
      }),
    )
    return {
      data: items.filter(Boolean),
    }
  },

  getManyReference: async (resource, params) => {
    const filter = { ...params.filter, [params.target]: params.id }
    const qs = buildQueryParams(resource, { ...params, filter })
    const res = await api.get<PageResponse<any>>(`/admin/${resource}?${qs}`)
    return {
      data: res.items,
      total: res.total,
    }
  },

  create: async (resource, params) => {
    if (resource !== 'users') {
      throw new Error(`资源 ${resource} 不支持创建操作`)
    }
    const res = await api.post<any>('/admin/users', params.data)
    return {
      data: res,
    }
  },

  update: async (resource, params) => {
    if (resource === 'users') {
      const { username, status } = params.data
      const payload: { username?: string; status?: 'active' | 'disabled' } = {}
      if (username !== undefined) payload.username = username
      if (status !== undefined) payload.status = status
      const res = await api.patch<any>(`/admin/users/${params.id}`, payload)
      return { data: res }
    }
    if (resource === 'assets') {
      const { title } = params.data
      const res = await api.patch<any>(`/admin/assets/${params.id}`, { title })
      return { data: res }
    }
    throw new Error(`资源 ${resource} 不支持直接更新操作`)
  },

  updateMany: () => Promise.reject(new Error('批量更新操作未开放')),

  delete: async (resource, params) => {
    if (resource === 'assets') {
      const res = await api.delete<any>(`/admin/assets/${params.id}`)
      return { data: res }
    }
    throw new Error(`资源 ${resource} 的删除操作须通过专用凭据确认流程`)
  },

  deleteMany: async (resource, params) => {
    if (resource === 'assets') {
      const res = await api.post<BatchDeleteOut>('/admin/assets/batch-delete', {
        asset_ids: params.ids,
      })
      return { data: res.deleted }
    }
    throw new Error(`资源 ${resource} 不支持批量删除操作`)
  },

  // ---- 专用业务动作 ----
  resetPassword: async (userId: string, password: string): Promise<void> => {
    await api.post<void>(`/admin/users/${userId}/reset-password`, { password })
  },

  getDeletionPreview: async (userId: string): Promise<AccountDeletionPreview> => {
    return api.get<AccountDeletionPreview>(`/admin/users/${userId}/deletion-preview`)
  },

  confirmDeleteUser: async (
    userId: string,
    confirmUsername: string,
    previewToken: string,
  ): Promise<DeletionOperationOut> => {
    return api.delete<DeletionOperationOut>(`/admin/users/${userId}`, {
      headers: {
        'X-Deletion-Preview': previewToken,
      },
      body: JSON.stringify({ confirm_username: confirmUsername }),
    })
  },

  getDeletionOperation: async (operationId: string): Promise<DeletionOperationOut> => {
    return api.get<DeletionOperationOut>(`/admin/deletion-operations/${operationId}`)
  },

  retryDeletionOperation: async (operationId: string): Promise<DeletionOperationOut> => {
    return api.post<DeletionOperationOut>(`/admin/deletion-operations/${operationId}/retry`)
  },

  batchDeleteAssets: async (assetIds: string[]): Promise<BatchDeleteOut> => {
    return api.post<BatchDeleteOut>('/admin/assets/batch-delete', {
      asset_ids: assetIds,
    })
  },
}
