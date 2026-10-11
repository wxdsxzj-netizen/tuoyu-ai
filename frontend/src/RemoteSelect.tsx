/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react'
import { Button, Select } from 'antd'
import { api } from './api/client'

// Remote search and continuation are shared by object and team-member pickers.
export default function RemoteSelect({ url, params = {}, sizeKey = 'size', label = 'name', value, onChange, mode, exclude, ...props }: {
  url: string; params?: Record<string, unknown>; sizeKey?: string; label?: string | ((row: any) => string)
  exclude?: string; value?: any; onChange?: (value: any, rows?: any) => void; mode?: 'multiple'; [key: string]: any
}) {
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  const selected = useRef<any[]>([])
  const filterKey = JSON.stringify(params)
  useEffect(() => { setPage(1); setRows([]); setQuery('') }, [url, filterKey])
  useEffect(() => {
    let active = true
    setBusy(true); setFailed(false)
    api.get(url, { params: { ...JSON.parse(filterKey), q: query || undefined, page, [sizeKey]: 20 } }).then(r => {
      if (!active) return
      const items = Array.isArray(r.data) ? r.data : r.data.items || []
      setRows(previous => page === 1 ? items : [...new Map([...previous, ...items].map(row => [row.id, row])).values()])
      setTotal(Array.isArray(r.data) ? items.length : r.data.total)
    }).catch(() => { if (active) setFailed(true) }).finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [url, filterKey, query, page, sizeKey, retry])
  const ids = Array.isArray(value) ? value : [value]
  const options = [...new Map([...selected.current.filter(row => ids.includes(row.id)), ...rows].map(row => [row.id, row])).values()]
  return <Select {...props} mode={mode} value={value} loading={busy} showSearch filterOption={false}
    onSearch={text => { setQuery(text); setPage(1) }}
    onChange={next => { selected.current = options.filter(row => (Array.isArray(next) ? next : [next]).includes(row.id)); onChange?.(next, mode ? selected.current : selected.current[0]) }}
    options={options.filter(row => row.id !== exclude).map(row => ({ value: row.id, label: typeof label === 'function' ? label(row) : row[label] || row.email || row.id }))}
    popupRender={menu => <>{menu}{failed ? <Button block onClick={() => setRetry(n => n + 1)}>加载失败，重试</Button> : rows.length < total && <Button block disabled={busy} onMouseDown={event => event.preventDefault()} onClick={() => setPage(n => n + 1)}>加载更多</Button>}</>} />
}
