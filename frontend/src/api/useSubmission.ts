import { useRef, useState } from 'react'

interface Intent<T> { request_id: string; payload: T }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`
  return JSON.stringify(value)
}

export function useSubmission<T>(scope: string) {
  const key = `tl-submission:${scope}`
  const read = (): Intent<T> | null => {
    const raw = sessionStorage.getItem(key)
    return raw ? JSON.parse(raw) : null
  }
  const [pending, setPending] = useState<Intent<T> | null>(read)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>()
  const busy = useRef(false)
  const run = async <R,>(payload: T, send: (requestId: string) => Promise<R>): Promise<R | undefined> => {
    if (busy.current) return
    busy.current = true; setLoading(true); setError(undefined)
    let wasPending = false
    try {
      const old = read()
      wasPending = !!old
      if (old && canonical(old.payload) !== canonical(payload)) throw new Error('上次提交结果待确认，请使用原内容重试。')
      const intent = old || { request_id: crypto.randomUUID(), payload }
      sessionStorage.setItem(key, JSON.stringify(intent))
      setPending(intent)
      const result = await send(intent.request_id)
      sessionStorage.removeItem(key); setPending(null)
      return result
    } catch (failure) {
      const e = failure as { message?: string; response?: { status?: number; data?: { error?: { message?: string } } } }
      if (!wasPending && [400, 403, 404, 409, 422].includes(e.response?.status || 0)) {
        sessionStorage.removeItem(key); setPending(null)
      }
      setError(e.response?.data?.error?.message || e.message || '提交未完成，请重试原请求。')
    } finally { busy.current = false; setLoading(false) }
  }
  return { pending, loading, error, run }
}
