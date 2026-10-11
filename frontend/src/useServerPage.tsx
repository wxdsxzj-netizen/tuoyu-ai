/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button } from 'antd'
import { api } from './api/client'

export interface ServerPage<T> { items: T[]; total: number; [key: string]: any }
export function useServerPage(url: string, filters: Record<string, unknown> = {}, sizeKey = 'size', enabled = true) {
  const key = JSON.stringify(filters)
  const [position, setPosition] = useState({ key, page: 1 })
  const current = position.key === key ? position.page : 1
  const [data, setData] = useState<ServerPage<any>>({ items: [], total: 0 })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const [revision, setRevision] = useState(0)
  const reload = useCallback(() => setRevision(v => v + 1), [])
  useEffect(() => {
    let active = true
    setData({ items: [], total: 0 }); setError(false)
    if (!enabled) { setLoading(false); return }
    setLoading(true)
    api.get(url, { params: { ...JSON.parse(key), page: current, [sizeKey]: 20 } }).then(response => {
      if (!active) return
      const result = Array.isArray(response.data) ? { items: response.data, total: response.data.length } : response.data
      const total = result.total ?? result.items?.length ?? 0
      const last = Math.max(1, Math.ceil(total / 20))
      if (current > last) { setPosition({ key, page: last }); return }
      setData({ ...result, items: result.items || [], total })
    }).catch(() => { if (active) setError(true) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [url, key, current, sizeKey, enabled, revision])
  return { data, rows: data.items, error, loading, reload,
    pagination: { current, pageSize: 20, total: data.total, showSizeChanger: false, onChange: (page: number) => setPosition({ key, page }) } }
}
export function PageError({ page }: { page: { error: boolean; reload: () => void } }) {
  return page.error ? <Alert type="error" showIcon message="列表加载失败" action={<Button onClick={page.reload}>重试</Button>} style={{ marginBottom: 12 }} /> : null
}
