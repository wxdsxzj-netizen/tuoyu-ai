import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Space, Spin } from 'antd'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'

export default function MailboxOAuthCallback() {
  const { provider } = useParams()
  const payload = useRef<{ state: string; code?: string; error?: string }>()
  if (!payload.current) {
    const query = new URLSearchParams(window.location.search)
    payload.current = { state: query.get('state') || '', code: query.get('code') || undefined, error: query.get('error') || undefined }
  }
  const [status, setStatus] = useState<'loading' | 'success' | 'cancelled' | 'error'>('loading')
  const [address, setAddress] = useState('')
  const [detail, setDetail] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    // Authorization codes must not remain in browser history or subsequent referrers.
    window.history.replaceState(window.history.state, '', window.location.pathname)
    let active = true
    const timer = setTimeout(async () => {
      if (!['google', 'microsoft'].includes(provider || '') || !payload.current?.state) {
        setDetail('授权信息缺失或已过期，请返回发件箱重新授权。'); setStatus('error'); return
      }
      setStatus('loading')
      try {
        const response = await api.post(`/mailboxes/oauth/${provider}:callback`, payload.current)
        if (!active) return
        setAddress(response.data.address || '')
        setStatus(response.data.cancelled ? 'cancelled' : 'success')
      } catch (failure) {
        if (!active) return
        const error = failure as { response?: { data?: { error?: { message?: string } } } }
        setDetail(error.response?.data?.error?.message || '连接结果尚未确认，可重试查询；如授权已失效，请返回发件箱重新授权。')
        setStatus('error')
      }
    }, 0)
    return () => { active = false; clearTimeout(timer) }
  }, [provider, attempt])
  return <Card title="邮箱授权结果" style={{ maxWidth: 640, margin: '40px auto' }}>
    {status === 'loading' && <Spin tip="正在确认授权结果"><div style={{ height: 60 }} /></Spin>}
    {status === 'success' && <Alert showIcon type="success" message="邮箱授权已连接" description={`${address}；请在发件箱完成连接测试和域名检查。`} />}
    {status === 'cancelled' && <Alert showIcon type="info" message="已取消邮箱授权，本次没有绑定邮箱。" />}
    {status === 'error' && <Alert showIcon type="error" message="邮箱连接未完成" description={detail} />}
    <Space style={{ marginTop: 16 }}>
      <Link to="/mailboxes"><Button>返回发件箱</Button></Link>
      {status === 'error' && payload.current?.state && <Button onClick={() => setAttempt(value => value + 1)}>重试确认</Button>}
    </Space>
  </Card>
}
