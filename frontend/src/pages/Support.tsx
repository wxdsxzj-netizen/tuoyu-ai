/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, List, Modal, Progress, Select, Space, Table, Tabs, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth'

function Onboarding() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [o, setO] = useState<any>()
  useEffect(() => { api.get('/onboarding').then(r => setO(r.data)) }, [])
  if (!o) return null
  return <>
    <Progress percent={o.pct} style={{ maxWidth: 480 }} />
    <div style={{ margin: '6px 0 12px', color: '#888' }}>{t('b4.obLeft', { n: o.total - o.done, m: o.minutes_left })}</div>
    <List bordered dataSource={o.items} renderItem={(i: any) => <List.Item actions={[!i.done && <Button key="go" size="small" type="link" onClick={() => nav(i.path)}>{t('b4.go')}</Button>]}>
      <Checkbox checked={i.done} disabled /> <span style={{ marginLeft: 8, textDecoration: i.done ? 'line-through' : undefined }}>{t('b4.ob_' + i.key, { defaultValue: i.title })}</span>
      <Tag style={{ marginLeft: 8 }}>{i.minutes} {t('b4.min')}</Tag></List.Item>} />
  </>
}

function Assistant() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [r, setR] = useState<any>()
  return <>
    <Space.Compact style={{ width: '100%', maxWidth: 640 }}><Input value={q} onChange={e => setQ(e.target.value)} placeholder={t('b4.askPh')} onPressEnter={async () => setR((await api.post('/assistant', { q })).data)} />
      <Button type="primary" onClick={async () => setR((await api.post('/assistant', { q })).data)}>{t('b4.ask')}</Button></Space.Compact>
    {r && <Card size="small" style={{ marginTop: 12, maxWidth: 800 }}>
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{r.answer}</Typography.Paragraph>
      <Space wrap>{r.actions.map((a: any) => <Button key={a.label} onClick={() => nav(a.path, { state: { prefill: a.prefill } })}>{a.label} →</Button>)}</Space>
    </Card>}
  </>
}

function Tickets() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [cur, setCur] = useState<any>()
  const [reply, setReply] = useState('')
  const [form] = Form.useForm()
  const load = () => api.get('/tickets').then(r => { setRows(r.data); if (cur) setCur(r.data.find((x: any) => x.id === cur.id)) })
  useEffect(() => { load() }, [])
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b4.slaInfo')} />
    <Button type="primary" onClick={() => setOpen(true)} style={{ marginBottom: 12 }}>{t('b4.newTicket')}</Button>
    <Table rowKey="id" size="small" dataSource={rows} onRow={r => ({ onClick: () => setCur(r) })} columns={[
      { title: t('b4.ticketNo'), dataIndex: 'ticket_no' }, { title: t('b4.subject'), dataIndex: 'subject' },
      { title: t('b4.priority'), dataIndex: 'priority', render: p => <Tag color={{ P1: 'red', P2: 'orange', P3: 'blue', P4: 'default' }[p as string]}>{p}</Tag> },
      { title: t('b4.status'), dataIndex: 'status', render: s => <Tag>{t('b4.ts_' + s)}</Tag> },
      { title: t('b4.frDue'), dataIndex: 'first_response_due', render: (d, r: any) => <>{new Date(d).toLocaleString()} {r.breached?.length ? <Tag color="red">{t('b4.breached')}</Tag> : null}</> },
    ]} />
    <Modal open={open} title={t('b4.newTicket')} onCancel={() => setOpen(false)} onOk={async () => { const v = await form.validateFields(); await api.post('/tickets', v); setOpen(false); form.resetFields(); load() }}>
      <Form form={form} layout="vertical" initialValues={{ category: 'question', priority: 'P3' }}>
        <Form.Item name="subject" label={t('b4.subject')} rules={[{ required: true, min: 2 }]}><Input /></Form.Item>
        <Form.Item name="category" label={t('b4.category')}><Select options={['question', 'bug', 'billing', 'data', 'feature', 'security'].map(c => ({ value: c, label: t('b4.tc_' + c) }))} /></Form.Item>
        <Form.Item name="priority" label={t('b4.priority')} extra={t('b4.p1Note')}><Select options={['P1', 'P2', 'P3', 'P4'].map(p => ({ value: p, label: `${p} · ${t('b4.p_' + p)}` }))} /></Form.Item>
        <Form.Item name="body" label={t('b4.body')} rules={[{ required: true, min: 2 }]}><Input.TextArea rows={4} /></Form.Item>
      </Form>
    </Modal>
    <Modal open={!!cur} title={cur?.ticket_no + ' ' + (cur?.subject || '')} footer={null} onCancel={() => setCur(undefined)} width={640}>
      {cur?.messages.map((m: any, i: number) => <div key={i} style={{ marginBottom: 8 }}><Tag color={m.from === 'support' ? 'green' : 'default'}>{m.from === 'support' ? t('b4.supportTeam') : m.name}</Tag>
        <Typography.Text type="secondary">{new Date(m.at).toLocaleString()}</Typography.Text><div style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div></div>)}
      {cur && cur.status !== 'closed' && <Space.Compact style={{ width: '100%' }}><Input value={reply} onChange={e => setReply(e.target.value)} />
        <Button onClick={async () => { await api.post(`/tickets/${cur.id}/messages`, { text: reply }); setReply(''); load() }}>{t('b4.send')}</Button>
        <Button onClick={async () => { await api.post(`/tickets/${cur.id}/messages`, { text: reply || '已解决', close: true, csat: 5 }); setReply(''); load() }}>{t('b4.closeTicket')}</Button></Space.Compact>}
    </Modal>
  </>
}

function Account() {
  const { t } = useTranslation()
  const { can, me } = useAuth()
  const [hs, setHs] = useState<any>()
  const [cl, setCl] = useState<any>()
  const load = () => { if (can('billing.read')) api.get('/health-score').then(r => setHs(r.data)); if (can('billing.manage')) api.get('/tenant/closure').then(r => setCl(r.data)) }
  useEffect(() => { load() }, [])
  const slug = (me as any)?.tenant?.slug
  const close = () => { let confirm = '', reason = ''
    Modal.confirm({ title: t('b4.closureTitle'), width: 560, okButtonProps: { danger: true }, content: <>
      <Alert type="warning" showIcon message={t('b4.closureWarn')} style={{ marginBottom: 8 }} />
      <Input.TextArea placeholder={t('b4.reason')} onChange={e => { reason = e.target.value }} style={{ marginBottom: 8 }} />
      <Input placeholder={t('b4.typeName')} onChange={e => { confirm = e.target.value }} /></>,
      onOk: async () => { await api.post('/tenant/closure', { confirm, reason }); message.success(t('b1.done')); load() } }) }
  return <Space direction="vertical" style={{ width: '100%' }}>
    {hs && <Card size="small" title={t('b4.health')}><Space><Progress type="circle" size={80} percent={hs.score} status={hs.level === 'at_risk' ? 'exception' : undefined} /><Tag>{t('b4.hl_' + hs.level)}</Tag></Space>
      <Descriptions size="small" column={3} style={{ marginTop: 8 }} items={Object.entries(hs.parts).map(([k, v]) => ({ key: k, label: k, children: String(v) }))} /></Card>}
    <Card size="small" title={t('b4.legal')}><Space wrap>{['privacy', 'dpa', 'subprocessors', 'cookies'].map(d => <Button key={d} href={`/api/v1/public/legal/${slug}/${d}`} target="_blank">{t('b4.lg_' + d)}</Button>)}</Space>
      <div style={{ color: '#888', marginTop: 6 }}>{t('b4.legalNote')}</div></Card>
    <Card size="small" title={t('b4.status')}><Button href="/status" target="_blank">{t('b4.statusPage')}</Button></Card>
    {can('billing.manage') && <Card size="small" title={t('b4.closure')}>
      {cl?.status === 'pending' ? <Space><Alert type="error" showIcon message={t('b4.closurePending', { d: new Date(cl.purge_after).toLocaleDateString() })} />
        <Button onClick={async () => { await api.delete('/tenant/closure'); load() }}>{t('b4.cancelClosure')}</Button></Space>
        : <><Typography.Paragraph>{t('b4.closureDesc')}</Typography.Paragraph><Button danger onClick={close}>{t('b4.requestClosure')}</Button></>}</Card>}
  </Space>
}

export default function SupportPage() {
  const { t } = useTranslation()
  return <Card title={t('menu.support')}><Tabs items={[
    { key: 'ob', label: t('b4.onboarding'), children: <Onboarding /> },
    { key: 'as', label: t('b4.assistant'), children: <Assistant /> },
    { key: 'tk', label: t('b4.tickets'), children: <Tickets /> },
    { key: 'ac', label: t('b4.account'), children: <Account /> },
  ]} /></Card>
}
