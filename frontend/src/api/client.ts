import axios, { AxiosError } from 'axios'
import { message } from 'antd'
import { isPublicPage, isPublicRequest } from '../authPaths'

export const TOKEN_KEY = 'tl_token'
export const api = axios.create({ baseURL: '/api/v1' })

api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem(TOKEN_KEY)
  if (t && !isPublicRequest(cfg.url)) cfg.headers.Authorization = `Bearer ${t}`
  if (isPublicRequest(cfg.url)) delete cfg.headers.Authorization
  return cfg
})

interface ApiErr { error?: { code: string; message: string; trace_id: string } }

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError<ApiErr>) => {
    const e = err.response?.data?.error
    if (err.response?.status === 401 && !isPublicRequest(err.config?.url) && !err.config?.url?.includes('/auth/login')) {
      localStorage.removeItem(TOKEN_KEY)
      if (location.pathname !== '/login' && !isPublicPage(location.pathname)) {
        location.href = `/login?returnTo=${encodeURIComponent(location.pathname + location.search + location.hash)}`
      }
    }
    message.error(e ? `${e.message}（${e.code} · ${e.trace_id.slice(0, 8)}）` : '网络错误')
    return Promise.reject(err)
  },
)

export interface Me {
  user: { id: string; name: string; email: string; role_code: string; role_name: string }
  tenant: { id: string; name: string; slug: string; locale: string; timezone: string }
  permissions: string[]
}
export interface UserRow {
  id: string; email: string; name: string; role_code: string; role_name: string
  is_active: boolean; last_login_at: string | null; created_at: string; version: number
}
