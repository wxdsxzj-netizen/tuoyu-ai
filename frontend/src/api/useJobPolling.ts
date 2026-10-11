import { useCallback, useEffect, useState } from 'react'
import { api } from './client'

export function useJobPolling<T extends { status: string }>(url: string) {
  const [items, setItems] = useState<T[]>([])
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState(false)
  const reload = useCallback(() => setRevision(n => n + 1), [])
  useEffect(() => {
    let live = true
    let timer: number | undefined
    const refresh = async () => {
      try {
        const response = await api.get<T[]>(url)
        if (!live) return
        setItems(response.data); setError(false)
        if (response.data.some(j => ['queued', 'running'].includes(j.status))) timer = window.setTimeout(refresh, 1500)
      } catch {
        if (live) { setError(true); timer = window.setTimeout(refresh, 5000) }
      }
    }
    refresh()
    return () => { live = false; window.clearTimeout(timer) }
  }, [url, revision])
  return { items, reload, error }
}
