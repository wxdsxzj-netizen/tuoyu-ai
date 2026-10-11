/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Form, Input, Space, Tag } from 'antd'
import { useParams } from 'react-router-dom'
import axios from 'axios'

// 访客页面（嵌入到客户官网，英文）。只与本系统通信；不加载第三方脚本，不设追踪 Cookie。
const origin = () => { try { return document.referrer ? new URL(document.referrer).origin : location.origin } catch { return location.origin } }
const H = () => ({ 'X-Embed-Origin': origin() })
const errorText = (e: any) => e.response?.data?.error?.message_en || e.response?.data?.error?.message || 'Failed, please retry'

export default function WidgetEmbedPage() {
  const { key } = useParams()
  const [cfg, setCfg] = useState<any>()
  const [err, setErr] = useState<string>()
  const [retry, setRetry] = useState(0)
  const [cookieOk, setCookieOk] = useState(localStorage.getItem('tl_cookie') || '')
  useEffect(() => {
    let live = true
    setCfg(undefined); setErr(undefined)
    axios.get(`/api/v1/public/widgets/${key}/config`, { headers: H() }).then(r => { if (live) setCfg({ ...r.data, loadedKey: key }) }).catch(e => { if (live) setErr(errorText(e)) })
    return () => { live = false }
  }, [key, retry])
  if (err) return <Alert type="error" message={err} style={{ margin: 16 }} action={<Button onClick={() => setRetry(n => n + 1)}>Retry</Button>} />
  if (!cfg || cfg.loadedKey !== key) return null
  return <div style={{ padding: 12 }}>
    {cfg.cookie_banner && !cookieOk && <Alert style={{ marginBottom: 8 }} message={cfg.cookie_text} action={<Space direction="vertical">
      <Button size="small" type="primary" onClick={() => { localStorage.setItem('tl_cookie', 'necessary_only'); setCookieOk('necessary_only') }}>OK (necessary only)</Button></Space>} />}
    {cfg.kind === 'form' ? <FormW key={key} k={key!} cfg={cfg} cookie={cookieOk} /> : <ChatW key={key} k={key!} cfg={cfg} cookie={cookieOk} />}
    <div style={{ fontSize: 11, color: '#999', marginTop: 6 }}>No tracking cookies · No advertising</div>
  </div>
}

function FormW({ k, cfg, cookie }: { k: string, cfg: any, cookie: string }) {
  const [done, setDone] = useState<string>()
  const [err, setErr] = useState<string>()
  const [busy, setBusy] = useState(false)
  const sending = useRef(false)
  const [form] = Form.useForm()
  const submit = async () => {
    if (sending.current) return
    sending.current = true; setBusy(true); setErr(undefined)
    try {
      const v = await form.validateFields()
      const r = await axios.post(`/api/v1/public/widgets/${k}/submit`, { ...v, cookie_choice: cookie || 'necessary_only' }, { headers: H() })
      setDone(r.data.message)
    } catch (e: any) { if (!e.errorFields) setErr(errorText(e)) }
    finally { sending.current = false; setBusy(false) }
  }
  if (done) return <Alert type="success" message={done} />
  return <Card size="small" title="Send us an inquiry">{err && <Alert type="error" showIcon message={err} />}<Form form={form} layout="vertical" size="small" disabled={busy}>
    {cfg.fields.map((f: any) => <Form.Item key={f.key} name={f.key} label={f.label} rules={f.required ? [{ required: true }] : []}>
      {f.key === 'message' ? <Input.TextArea rows={3} /> : <Input type={f.key === 'email' ? 'email' : 'text'} />}</Form.Item>)}
    <Form.Item name="website" hidden><Input tabIndex={-1} autoComplete="off" /></Form.Item>
    <Form.Item name="consent" valuePropName="checked" rules={[{ validator: (_, v) => v ? Promise.resolve() : Promise.reject(new Error('Required')) }]}>
      <Checkbox>{cfg.consent_text}</Checkbox></Form.Item>
    <Button type="primary" onClick={submit} loading={busy} block>Send</Button>
  </Form></Card>
}

function ChatW({ k, cfg, cookie }: { k: string, cfg: any, cookie: string }) {
  const [tok, setTok] = useState(localStorage.getItem('tl_chat_' + k) || '')
  const [msgs, setMsgs] = useState<any[]>([])
  const [text, setText] = useState('')
  const [err, setErr] = useState<string>()
  const [busy, setBusy] = useState(false)
  const [closed, setClosed] = useState(false)
  const sending = useRef(false)
  const readVersion = useRef(0)
  const [form] = Form.useForm()
  useEffect(() => {
    if (!tok) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (!sending.current) {
        const generation = ++readVersion.current
        try {
          const r = await axios.get(`/api/v1/public/chat/${tok}`)
          if (live && generation === readVersion.current) { setMsgs(r.data.messages); setClosed(r.data.mode === 'closed'); setErr(undefined) }
        } catch (e: any) {
          if (live && generation === readVersion.current) {
            setErr(errorText(e))
            if ([401, 404, 410].includes(e.response?.status)) { localStorage.removeItem('tl_chat_' + k); setTok('') }
          }
        }
      }
      if (live) timer = setTimeout(poll, 5000)
    }
    poll(); return () => { live = false; clearTimeout(timer) }
  }, [tok, k])
  const start = async () => {
    if (sending.current) return
    sending.current = true; setBusy(true); setErr(undefined)
    try {
      const v = await form.validateFields()
      const r = await axios.post(`/api/v1/public/widgets/${k}/chat`, { ...v, cookie_choice: cookie || 'necessary_only' }, { headers: H() })
      localStorage.setItem('tl_chat_' + k, r.data.token); setTok(r.data.token); setMsgs(r.data.messages); setClosed(false)
    } catch (e: any) { if (!e.errorFields) setErr(errorText(e)) }
    finally { sending.current = false; setBusy(false) }
  }
  if (!tok) return <Card size="small" title="Chat with us">{err && <Alert type="error" showIcon message={err} />}<Form form={form} layout="vertical" size="small" disabled={busy}>
    <Form.Item name="name" label="Name"><Input /></Form.Item>
    <Form.Item name="email" label="Email (so we can reply if you leave)"><Input type="email" /></Form.Item>
    <Form.Item name="consent" valuePropName="checked" rules={[{ validator: (_, v) => v ? Promise.resolve() : Promise.reject(new Error('Required')) }]}><Checkbox>{cfg.consent_text}</Checkbox></Form.Item>
    <Button type="primary" block loading={busy} onClick={start}>Start chat</Button></Form></Card>
  const send = async () => {
    if (!text.trim() || sending.current || closed) return
    const tx = text
    sending.current = true; setBusy(true); setErr(undefined); readVersion.current++
    try {
      const r = await axios.post(`/api/v1/public/chat/${tok}/messages`, { text: tx })
      setText(''); setClosed(r.data.mode === 'closed')
      setMsgs(old => [...old, { from: 'visitor', text: tx }, ...(r.data.reply ? [r.data.reply] : [])])
    } catch (e) { setErr(errorText(e)) }
    finally { sending.current = false; setBusy(false) }
  }
  return <Card size="small" title="Chat">
    {err && <Alert type="error" showIcon message={err} />}
    {closed && <Alert type="info" message="This chat is closed. Please contact our sales team." />}
    <div style={{ height: 300, overflow: 'auto' }}>{msgs.map((m, i) => <div key={i} style={{ textAlign: m.from === 'visitor' ? 'right' : 'left', margin: '4px 0' }}>
      {m.from !== 'visitor' && <Tag color={m.from === 'bot' ? 'blue' : 'green'}>{m.from === 'bot' ? (m.label || 'Assistant') : (m.name || 'Sales')}</Tag>}
      <span style={{ background: '#f5f5f5', padding: '3px 8px', borderRadius: 6, display: 'inline-block', maxWidth: '80%' }}>{m.text}</span></div>)}</div>
    <Space.Compact style={{ width: '100%' }}><Input disabled={busy || closed} value={text} onChange={e => setText(e.target.value)} onPressEnter={send} placeholder="Type a message" /><Button type="primary" disabled={closed} loading={busy} onClick={send}>Send</Button></Space.Compact>
  </Card>
}
