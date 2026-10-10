/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Form, Input, Result, Table } from 'antd'
import { useParams } from 'react-router-dom'
import axios from 'axios'

/** 客户在线确认页（公开，无需登录；面向海外客户，使用英文）。 */
export default function ConfirmPage() {
  const { token } = useParams()
  const [q, setQ] = useState<any>()
  const [err, setErr] = useState<string>()
  const [done, setDone] = useState<string>()
  useEffect(() => { axios.get(`/public/confirm/${token}`).then((r) => setQ(r.data)).catch((e) => setErr(e.response?.data?.error?.code || 'ERROR')) }, [token])
  const submit = async (accept: boolean, v: any) => {
    const r = await axios.post(`/public/confirm/${token}`, { ...v, accept })
    setDone(r.data.status)
  }
  const [form] = Form.useForm()
  if (done) return <Result status="success" title={done === 'locked' ? 'Thank you, the quotation is confirmed.' : 'Thank you, your comments were sent to our sales team.'} />
  if (err) return <Result status="warning" title={err === 'CRM_CONFIRM_LINK_EXPIRED' ? 'This link has expired. Please contact your sales representative.' : 'Link not found.'} />
  if (!q) return null
  return (
    <Card style={{ maxWidth: 900, margin: '32px auto' }} title={`Quotation ${q.quote_no} (v${q.doc_version})`}>
      <Descriptions size="small" column={2}>
        <Descriptions.Item label="Trade term">{q.incoterm} {q.port}</Descriptions.Item>
        <Descriptions.Item label="Valid until">{q.valid_until}</Descriptions.Item>
        <Descriptions.Item label="Payment terms">{q.payment_terms}</Descriptions.Item>
        <Descriptions.Item label="Delivery">{q.delivery_terms}</Descriptions.Item>
      </Descriptions>
      <Table rowKey={(_, i) => String(i)} size="small" pagination={false} dataSource={q.items} columns={[
        { title: 'Item', render: (_: any, r: any) => `${r.name} ${r.sku || ''}` }, { title: 'Qty', render: (_: any, r: any) => `${r.qty} ${r.unit}` },
        { title: 'Unit price', dataIndex: 'unit_price' }, { title: 'Amount', render: (_: any, r: any) => (r.line_minor / 100).toFixed(2) },
      ]} />
      <Alert style={{ margin: '12px 0' }} message={`Total: ${q.currency} ${q.total.toFixed(2)}${q.discount_pct ? ` (discount ${q.discount_pct}%)` : ''}`} />
      <Form form={form} layout="vertical">
        <Form.Item name="name" label="Your name" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="email" label="Your email" rules={[{ required: true, type: 'email' }]}><Input /></Form.Item>
        <Form.Item name="comment" label="Comments (optional)"><Input.TextArea rows={3} /></Form.Item>
        <Button type="primary" onClick={async () => submit(true, await form.validateFields())}>Confirm quotation</Button>{' '}
        <Button onClick={async () => submit(false, await form.validateFields())}>Request changes</Button>
      </Form>
    </Card>
  )
}
