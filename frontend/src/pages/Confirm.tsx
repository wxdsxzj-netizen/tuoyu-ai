/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Descriptions, Form, Input, Result, Table } from 'antd'
import { useParams } from 'react-router-dom'
import axios from 'axios'

/** 客户在线确认页（公开，无需登录；面向海外客户，使用英文）。 */
export default function ConfirmPage() {
  const { token } = useParams()
  const [q, setQ] = useState<any>()
  const [err, setErr] = useState<string>()
  const [done, setDone] = useState<string>()
  const [submitError, setSubmitError] = useState<string>()
  const [busy, setBusy] = useState(false)
  const sending = useRef(false)
  const activeToken = useRef(token)
  activeToken.current = token
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let live = true
    setQ(undefined); setErr(undefined); setDone(undefined); setSubmitError(undefined); setBusy(false); sending.current = false
    axios.get(`/public/confirm/${token}`).then(r => { if (live) setQ(r.data) }).catch(e => { if (live) setErr(e.response?.data?.error?.code || 'ERROR') })
    return () => { live = false }
  }, [token, retry])
  const submit = async (accept: boolean) => {
    if (sending.current) return
    sending.current = true; setBusy(true); setSubmitError(undefined)
    try {
      const v = await form.validateFields()
      if (activeToken.current !== token) return
      const r = await axios.post(`/public/confirm/${token}`, { ...v, accept })
      if (activeToken.current === token) setDone(r.data.status)
    } catch (e: any) {
      if (!e.errorFields && activeToken.current === token) setSubmitError(e.response?.data?.error?.message_en || 'Unable to submit. Please retry or contact your sales representative.')
    } finally { if (activeToken.current === token) { sending.current = false; setBusy(false) } }
  }
  const [form] = Form.useForm()
  if (done) return <Result status="success" title={done === 'locked' ? 'Thank you, the quotation is confirmed.' : 'Thank you, your comments were sent to our sales team.'} />
  if (err) return <Result status="warning" title={err === 'CRM_CONFIRM_LINK_EXPIRED' ? 'This link has expired. Please contact your sales representative.' : err === 'COMMON_NOT_FOUND' ? 'Link not found.' : 'Unable to load this quotation.'} extra={<Button onClick={() => setRetry(n => n + 1)}>Retry</Button>} />
  if (!q) return null
  return (
    <Card style={{ maxWidth: 900, margin: '32px auto' }} title={`Quotation ${q.quote_no} (v${q.doc_version})`}>
      <Descriptions size="small" column={2}>
        <Descriptions.Item label="Trade term">{q.incoterm} {q.port}</Descriptions.Item>
        <Descriptions.Item label="Valid until">{q.valid_until}</Descriptions.Item>
        <Descriptions.Item label="Payment terms">{q.payment_terms}</Descriptions.Item>
        <Descriptions.Item label="Delivery">{q.delivery_terms}</Descriptions.Item>
      </Descriptions>
      <Table rowKey="rowKey" size="small" pagination={false} dataSource={q.items.map((item: any, index: number) => ({ ...item, rowKey: item.id || String(index) }))} columns={[
        { title: 'Item', render: (_: any, r: any) => `${r.name} ${r.sku || ''}` }, { title: 'Qty', render: (_: any, r: any) => `${r.qty} ${r.unit}` },
        { title: 'Unit price', dataIndex: 'unit_price' }, { title: 'Amount', render: (_: any, r: any) => (r.line_minor / 100).toFixed(2) },
      ]} />
      <Alert style={{ margin: '12px 0' }} message={`Total: ${q.currency} ${q.total.toFixed(2)}${q.discount_pct ? ` (discount ${q.discount_pct}%)` : ''}`} />
      {submitError && <Alert type="error" showIcon message={submitError} />}
      <Form form={form} layout="vertical" disabled={busy}>
        <Form.Item name="name" label="Your name" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="email" label="Your email" rules={[{ required: true, type: 'email' }]}><Input /></Form.Item>
        <Form.Item name="comment" label="Comments (optional)"><Input.TextArea rows={3} /></Form.Item>
        <Button type="primary" loading={busy} onClick={() => submit(true)}>Confirm quotation</Button>{' '}
        <Button disabled={busy} onClick={() => submit(false)}>Request changes</Button>
      </Form>
    </Card>
  )
}
