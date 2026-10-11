import { useRef, useState } from 'react'
import { Alert, Button, Modal, Space, Spin } from 'antd'
import { api } from './api/client'

type Provider = 'google' | 'microsoft'
interface OAuthInfo { stub: boolean; authorize_url?: string | null; note?: string; next_steps?: string[] }
const hosts: Record<Provider, string> = { google: 'accounts.google.com', microsoft: 'login.microsoftonline.com' }
export function authorizationUrl(provider: Provider, info?: OAuthInfo) {
  if (!info || info.stub || !info.authorize_url) return null
  try {
    const url = new URL(info.authorize_url)
    return url.protocol === 'https:' && url.hostname === hosts[provider] && !url.username && !url.password ? url.href : null
  } catch { return null }
}

export default function OAuthConnect({ provider, children }: { provider: Provider; children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const [info, setInfo] = useState<OAuthInfo>()
  const generation = useRef(0)
  const cancel = () => { generation.current++; setOpen(false); setLoading(false); setInfo(undefined); setError(false) }
  const start = async () => {
    const request = ++generation.current
    setOpen(true); setLoading(true); setError(false); setInfo(undefined)
    try {
      const response = await api.get(`/mailboxes/oauth/${provider}:start`)
      if (request === generation.current) setInfo(response.data)
    } catch {
      if (request === generation.current) setError(true)
    } finally {
      if (request === generation.current) setLoading(false)
    }
  }
  const url = authorizationUrl(provider, info)
  return <>
    <Button onClick={start} disabled={loading}>{children}</Button>
    <Modal destroyOnClose open={open} title={`${provider === 'google' ? 'Google' : 'Microsoft'} 邮箱授权`} onCancel={cancel}
      footer={<Space><Button onClick={cancel}>关闭</Button>{error && <Button onClick={start}>重试</Button>}
        {url && <Button type="primary" href={url}>继续服务商授权</Button>}</Space>}>
      {loading && <Spin />}
      {error && <Alert type="error" showIcon message="无法获取授权信息，请重试。" />}
      {info && <>
        <Alert type={url ? 'info' : 'warning'} showIcon message={info.note || (url ? '继续在服务商完成授权。' : '当前尚未接通真实邮箱。')} />
        {!url && <p>本次没有绑定邮箱。你可以关闭后通过 SMTP / IMAP 入口连接邮箱。</p>}
        {(info.next_steps || []).length > 0 && <ol>{info.next_steps!.map((step, i) => <li key={i}>{step}</li>)}</ol>}
      </>}
    </Modal>
  </>
}
