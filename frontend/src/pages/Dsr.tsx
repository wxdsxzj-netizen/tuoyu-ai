/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tag, Timeline, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

const PAGE_SIZE = 20
const ST_COLOR: Record<string, string> = { received: 'blue', verifying: 'gold', pending_info: 'orange', processing: 'purple', completed: 'green', rejected: 'default' }

export default function DsrPage() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [f, setF] = useState<any>({ page: 1, status: 'open' })
  const [creating, setCreating] = useState(false)
  const [cur, setCur] = useState<any>()
  const [found, setFound] = useState<any>()
  const [code, setCode] = useState<string>()
  const load = useCallback(async () => {
    const r = await api.get('/dsr', { params: { ...f, size: PAGE_SIZE } }); setRows(r.data.items); setTotal(r.data.total)
  }, [f])
  useEffect(() => { load() }, [load])
  const act = async (action: string, body: any = {}) => {
    const r = await api.post(`/dsr/${cur.id}:${action}`, body)
    setCur(r.data.request)
    if (r.data.verification_code) setCode(r.data.verification_code)
    if (r.data.data) setFound(r.data.data)
    if (r.data.result?.export) setFound(r.data.result.export)
    message.success(t('p3.done')); load()
  }
  const ask = (title: string, field: string, action: string, required = true) => {
    let v = ''
    Modal.confirm({ title, content: <Input.TextArea rows={3} onChange={(e) => { v = e.target.value }} />,
      onOk: () => (required && !v ? Promise.reject() : act(action, { [field]: v })) })
  }
  const cols = [
    { title: t('p3.dsrType'), dataIndex: 'type', render: (v: string) => <Tag>{t(`p3.dt_${v}`)}</Tag> },
    { title: t('p3.subject'), dataIndex: 'subject_email', render: (v: string) => v || <Typography.Text type="secondary">{t('p3.erased')}</Typography.Text> },
    { title: t('p3.channel'), dataIndex: 'channel', render: (v: string) => t(`p3.ch_${v}`) },
    { title: t('p3.status'), dataIndex: 'status', render: (v: string) => <Tag color={ST_COLOR[v]}>{t(`p3.ds_${v}`)}</Tag> },
    { title: t('p3.daysLeft'), dataIndex: 'days_left', render: (v: number, r: any) => ['completed', 'rejected'].includes(r.status) ? '—'
      : <Tag color={v <= 2 ? 'red' : v <= 10 ? 'orange' : 'green'}>{v} {t('p3.days')}</Tag> },
    { title: t('p3.receivedAt'), dataIndex: 'received_at', render: (v: string) => new Date(v).toLocaleString() },
    { title: '', render: (_: any, r: any) => <a onClick={() => { setCur(r); setFound(undefined); setCode(undefined) }}>{t('p3.handle')}</a> },
  ]
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Select value={f.status} style={{ width: 140 }} onChange={(v) => setF({ ...f, status: v, page: 1 })}
          options={[{ value: 'open', label: t('p3.open') }, ...['completed', 'rejected'].map((s) => ({ value: s, label: t(`p3.ds_${s}`) }))]} />
        <Input.Search allowClear placeholder={t('p3.searchDsr')} onSearch={(q) => setF({ ...f, q: q || undefined, page: 1 })} />
        <Button type="primary" onClick={() => setCreating(true)}>{t('p3.newDsr')}</Button>
      </Space>
      <Alert style={{ marginBottom: 12 }} type="info" showIcon message={t('p3.dsrHint')} />
      <Table rowKey="id" columns={cols} dataSource={rows} pagination={{ total, current: f.page, pageSize: PAGE_SIZE, showSizeChanger: false, onChange: (p) => setF({ ...f, page: p }) }} />
      <Modal open={creating} title={t('p3.newDsr')} footer={null} onCancel={() => setCreating(false)} destroyOnClose>
        <Form layout="vertical" onFinish={async (v) => {
          const body = { ...v, rectify_changes: v.type === 'rectify' && v.title ? { title: v.title } : undefined }
          delete body.title
          await api.post('/dsr', body); setCreating(false); load()
        }}>
          <Form.Item name="type" label={t('p3.dsrType')} rules={[{ required: true }]}>
            <Select options={['access', 'delete', 'rectify', 'object'].map((x) => ({ value: x, label: t(`p3.dt_${x}`) }))} />
          </Form.Item>
          <Form.Item name="subject_email" label={t('p3.subject')} rules={[{ required: true, type: 'email' }]}><Input /></Form.Item>
          <Form.Item name="requester_name" label={t('p3.requester')}><Input /></Form.Item>
          <Form.Item name="channel" label={t('p3.channel')} initialValue="privacy_email">
            <Select options={['privacy_email', 'form', 'tenant', 'manual'].map((x) => ({ value: x, label: t(`p3.ch_${x}`) }))} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate>{({ getFieldValue }) => getFieldValue('type') === 'rectify' &&
            <Form.Item name="title" label={t('p3.rectifyTitle')}><Input /></Form.Item>}</Form.Item>
          <Form.Item name="details" label={t('p3.details')}><Input.TextArea rows={3} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('p3.submit')}</Button>
        </Form>
      </Modal>
      <Drawer open={!!cur} width={720} title={cur && `${t(`p3.dt_${cur.type}`)} · ${cur.subject_email || t('p3.erased')}`} onClose={() => setCur(undefined)}>
        {cur && <Space direction="vertical" style={{ width: '100%' }}>
          <Descriptions size="small" bordered column={2}>
            <Descriptions.Item label={t('p3.status')}><Tag color={ST_COLOR[cur.status]}>{t(`p3.ds_${cur.status}`)}</Tag></Descriptions.Item>
            <Descriptions.Item label={t('p3.dueAt')}>{new Date(cur.due_at).toLocaleDateString()}（{t('p3.daysLeft')} {cur.days_left}）</Descriptions.Item>
            <Descriptions.Item label={t('p3.verified')}>{cur.verified_at ? `${cur.verification_method} · ${new Date(cur.verified_at).toLocaleString()}` : '—'}</Descriptions.Item>
            <Descriptions.Item label={t('p3.pausedDays')}>{cur.paused_days}</Descriptions.Item>
            {cur.details && <Descriptions.Item label={t('p3.details')} span={2}>{cur.details}</Descriptions.Item>}
            {cur.result && <Descriptions.Item label={t('p3.result')} span={2}><code>{JSON.stringify(cur.result)}</code></Descriptions.Item>}
            {cur.reject_reason && <Descriptions.Item label={t('p3.rejectReason')} span={2}>{cur.reject_reason}</Descriptions.Item>}
          </Descriptions>
          {code && <Alert type="warning" showIcon message={`${t('p3.codeIs')} ${code}`} description={t('p3.codeHint')} />}
          <Space wrap>
            {['received', 'verifying', 'pending_info'].includes(cur.status) && <Button onClick={() => act('start-verification')}>{t('p3.startVerify')}</Button>}
            {cur.status === 'verifying' && <Button onClick={() => ask(t('p3.enterCode'), 'code', 'verify')}>{t('p3.enterCode')}</Button>}
            {cur.status === 'verifying' && <Button onClick={() => ask(t('p3.otherMethod'), 'method', 'verify')}>{t('p3.otherMethod')}</Button>}
            {['received', 'verifying'].includes(cur.status) && <Button onClick={() => ask(t('p3.requestInfo'), 'note', 'request-info')}>{t('p3.requestInfo')}</Button>}
            {cur.status === 'pending_info' && <Button onClick={() => act('resume')}>{t('p3.resume')}</Button>}
            {cur.status === 'processing' && <Button onClick={() => act('find')}>{t('p3.findData')}</Button>}
            {cur.status === 'processing' && <Button type="primary" danger={cur.type === 'delete'} onClick={() => Modal.confirm({
              title: t(`p3.exec_${cur.type}`), content: t('p3.execWarn'), onOk: () => act('execute') })}>{t(`p3.exec_${cur.type}`)}</Button>}
            {cur.status === 'processing' && cur.result && <Button type="primary" onClick={() => act('complete')}>{t('p3.complete')}</Button>}
            {!['completed', 'rejected'].includes(cur.status) && <Button danger onClick={() => ask(t('p3.reject'), 'reason', 'reject')}>{t('p3.reject')}</Button>}
          </Space>
          {found && <pre style={{ maxHeight: 280, overflow: 'auto', background: '#fafafa', padding: 8 }}>{JSON.stringify(found, null, 2)}</pre>}
          <Timeline items={(cur.timeline || []).map((e: any) => ({ children: `${new Date(e.at).toLocaleString()} · ${t(`p3.ev_${e.action}`, { defaultValue: String(e.action) })}` }))} />
        </Space>}
      </Drawer>
    </>
  )
}
