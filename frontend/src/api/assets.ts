import { api, apiError } from '@/api/client'

export type AssetKind =
  | 'original'
  | 'generated'
  | 'subject'
  | 'background'
  | 'mask'
  | 'marketing'
  | 'export'

export type Asset = {
  id: string
  kind: AssetKind
  source: 'upload' | 'generate' | 'tool'
  image_format: string
  width: number
  height: number
  size_bytes: number
  has_alpha: boolean
  title?: string | null
  created_at: string
  url: string
}

export type BatchSkippedItem = {
  id: string
  code: string
  reason: string
}

export type BatchDeleteOut = {
  deleted: string[]
  skipped: BatchSkippedItem[]
}

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
export const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp']

export type LibraryGroup = {
  session_id: string | null
  title: string
  updated_at: string
  cover: Asset
  assets: Asset[]
}

export const assetsApi = {
  async upload(file: File): Promise<Asset> {
    const body = new FormData()
    body.append('file', file)

    const response = await fetch('/api/assets', { method: 'POST', body })
    if (!response.ok) throw await apiError(response, '上传失败')
    return response.json()
  },

  list: (limit = 50) => api.get<Asset[]>(`/assets?limit=${limit}`),

  library: (limit = 50) => api.get<LibraryGroup[]>(`/assets/library?limit=${limit}`),

  patch: (id: string, title: string | null) => api.patch<Asset>(`/assets/${id}`, { title }),

  remove: (id: string) => api.delete<void>(`/assets/${id}`),

  batchRemove: (asset_ids: string[]) =>
    api.post<BatchDeleteOut>('/assets/batch-delete', { asset_ids }),
}
