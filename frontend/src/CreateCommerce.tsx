/* eslint-disable @typescript-eslint/no-explicit-any */
import { useRef, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select } from 'antd'
import { api } from './api/client'
import { useAuth } from './auth'
import RemoteSelect from './RemoteSelect'

export default function CreateCommerce({ kind, onSaved }: { kind: 'samples' | 'orders'; onSaved: () => void }) {
  const { can, me } = useAuth()
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [expired, setExpired] = useState(false)
  const storageKey = `tl-create:${me?.tenant.id}:${me?.user.id}:${kind}`
  const [form] = Form.useForm()
  const account = Form.useWatch('account_id', form)
  const quote = Form.useWatch('quote_id', form)
  const busy = useRef(false)
  const intent = useRef<{ key: string; body: any; values: any; created: number }>()
  const samples = kind === 'samples'
  const submit = async () => {
    if (busy.current || expired) return
    busy.current = true; setSaving(true); setError(false)
    try {
      const values = await form.validateFields()
      const body = samples ? { account_id: values.account_id, items: values.items, fee_type: values.fee_type, fee_minor: Math.round((values.fee || 0) * 100) }
        : { account_id: values.account_id, quote_id: values.quote_id || undefined, currency: values.currency,
          ...(values.quote_id ? {} : { total_minor: Math.round(values.amount * 100) }) }
      const request = intent.current || { key: crypto.randomUUID(), body, values, created: Date.now() }
      sessionStorage.setItem(storageKey, JSON.stringify(request))
      intent.current = request
      await api.post('/' + kind, request.body, { headers: { 'Idempotency-Key': request.key } })
      sessionStorage.removeItem(storageKey); intent.current = undefined; setUncertain(false); setOpen(false); form.resetFields(); onSaved()
    } catch (failure: any) {
      if (failure?.errorFields) return
      setError(true)
      if ([400, 403, 404, 409, 422].includes(failure?.response?.status) && !uncertain) { intent.current = undefined; sessionStorage.removeItem(storageKey) }
      else if (intent.current) setUncertain(true)
    } finally { busy.current = false; setSaving(false) }
  }
  if (!can('order.manage')) return null
  return <>
    <Button type="primary" style={{ marginBottom: 12 }} onClick={() => {
      const saved = sessionStorage.getItem(storageKey)
      if (saved && !intent.current) {
        try { intent.current = JSON.parse(saved) } catch { setError(true); return }
      }
      if (intent.current) {
        form.setFieldsValue(intent.current.values); setUncertain(true); setError(true)
        setExpired(Date.now() - intent.current.created > 23 * 60 * 60 * 1000)
      } else { form.resetFields(); setError(false); setUncertain(false); setExpired(false) }
      setOpen(true)
    }}>{samples ? '申请样品' : '创建订单'}</Button>
    <Modal open={open} title={samples ? '申请样品' : '创建订单'} onOk={submit} confirmLoading={saving} okButtonProps={{ disabled: expired }}
      closable={!saving} maskClosable={!saving} cancelButtonProps={{ disabled: saving }} onCancel={() => setOpen(false)}>
      {expired && <Alert type="warning" message="原请求已超过安全重试窗口，请先核对现有记录并由管理员处理，不能重新自动创建。" />}
      {error && <Alert type="error" message={uncertain ? '结果待确认，请重试原请求或核对记录，勿重复创建。' : '保存失败，请核对并重试。'} />}
      <Form form={form} layout="vertical" disabled={saving || uncertain} initialValues={{ currency: 'USD', fee_type: 'free', fee: 0, items: [{ name: '', qty: 1 }] }}>
        <Form.Item name="account_id" label="客户" rules={[{ required: true }]}><RemoteSelect url="/accounts" onChange={() => form.setFieldValue('quote_id', undefined)} /></Form.Item>
        {samples ? <>
          <Form.List name="items" rules={[{ validator: (_, items) => items?.length ? Promise.resolve() : Promise.reject(new Error('至少一项样品')) }]}>{(fields, { add, remove }, { errors }) => <>
            {fields.map(field => <div key={field.key}>
              <Form.Item name={[field.name, 'name']} label="品名" rules={[{ required: true }]}><Input /></Form.Item>
              <Form.Item name={[field.name, 'qty']} label="数量" rules={[{ required: true }]}><InputNumber min={1} precision={0} /></Form.Item>
              {fields.length > 1 && <Button onClick={() => remove(field.name)}>删除样品项</Button>}
            </div>)}<Button onClick={() => add({ name: '', qty: 1 })}>添加样品项</Button><Form.ErrorList errors={errors} />
          </>}</Form.List>
          <Form.Item name="fee_type" label="费用方式"><Select options={[{ value: 'free', label: '免费' }, { value: 'paid', label: '付费' }, { value: 'collect', label: '到付' }]} /></Form.Item>
          <Form.Item name="fee" label="费用金额"><InputNumber min={0} precision={2} /></Form.Item>
        </> : <>
          <Form.Item name="quote_id" label="关联报价（可选）"><RemoteSelect disabled={!account} allowClear url="/quotes" params={{ account_id: account }} label={(row: any) => `${row.quote_no} · ${row.status}`} /></Form.Item>
          <Form.Item name="currency" label="币种" rules={[{ required: !quote, pattern: /^[A-Z]{3}$/ }]}><Input disabled={!!quote} maxLength={3} /></Form.Item>
          <Form.Item name="amount" label="订单金额" rules={[{ required: !quote }]}><InputNumber disabled={!!quote} min={0.01} precision={2} /></Form.Item>
          {quote && <Alert type="info" message="金额和币种取自所选报价；服务端会核验报价状态与客户。" />}
        </>}
      </Form>
    </Modal>
  </>
}
