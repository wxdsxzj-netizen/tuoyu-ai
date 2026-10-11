/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Form, InputNumber, Modal, Row, Select, Space, Statistic, Switch, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import RemoteSelect from '../RemoteSelect'
import { useAuth } from '../auth'

function Plan() {
  const { t } = useTranslation()
  const [s, setS] = useState<any>()
  const [plans, setPlans] = useState<any[]>([])
  useEffect(() => { api.get('/billing/subscription').then(r => setS(r.data)); api.get('/billing/plans').then(r => setPlans(r.data)) }, [])
  if (!s) return null
  return <>
    {s.plan ? <Descriptions bordered size="small" column={3} items={[
      { key: 'p', label: t('b4.plan'), children: <>{s.plan.name} {s.pending_plan && <Tag color="orange">{t('b4.pendingPlan', { p: s.pending_plan })}</Tag>}</> },
      { key: 's', label: t('b4.seats'), children: `${s.active_users} / ${s.seats}` },
      { key: 'per', label: t('b4.period'), children: `${s.period_start} ~ ${s.period_end}` },
      { key: 'c', label: t('b4.included'), children: s.plan.included_credits }, { key: 'r', label: t('b4.rollover'), children: `${s.plan.rollover_pct}%` },
      { key: 'f', label: t('b4.monthlyFee'), children: `¥${s.plan.monthly_fee}` }]} />
      : <Alert type="info" showIcon message={s.note} />}
    <Row gutter={12} style={{ marginTop: 12 }}>{plans.map(p => <Col span={8} key={p.code}><Card size="small" title={p.name} extra={s.plan?.code === p.code && <Tag color="blue">{t('b4.current')}</Tag>}>
      <Statistic value={p.monthly_fee} prefix="¥" suffix={t('b4.perMonth')} />
      <div>{t('b4.planLine', { seats: p.included_seats, credits: p.included_credits, seat: p.seat_fee })}</div></Card></Col>)}</Row>
    <div style={{ color: '#888', marginTop: 8 }}>{t('b4.planNote')}</div>
  </>
}

function Budgets() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [deps, setDeps] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const scope = Form.useWatch('scope', form)
  const load = () => api.get('/budgets').then(r => setRows(r.data))
  useEffect(() => { load(); api.get('/users').then(r => setUsers(r.data.items || r.data)).catch(() => 0); api.get('/departments').then(r => setDeps(r.data.items || r.data)).catch(() => 0) }, [])
  const name = (b: any) => b.scope === 'user' ? users.find(u => u.id === b.scope_id)?.name : b.scope === 'department' ? deps.find(d => d.id === b.scope_id)?.name : t('b4.wholeTenant')
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b4.budgetInfo')} />
    {can('budget.manage') && <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setOpen(true)}>{t('b4.addBudget')}</Button>}
    <Table rowKey="id" size="small" dataSource={rows} pagination={false} columns={[
      { title: t('b4.scope'), dataIndex: 'scope', render: s => <Tag>{t('b4.sc_' + s)}</Tag> }, { title: t('b4.target'), render: (_: any, b: any) => name(b) || b.scope_id },
      { title: t('b4.limit'), dataIndex: 'monthly_limit' }, { title: t('b4.used'), dataIndex: 'used' },
      { title: '%', dataIndex: 'pct', render: p => <Tag color={p >= 100 ? 'red' : p >= 80 ? 'orange' : 'green'}>{p}%</Tag> },
      { title: t('b4.hard'), dataIndex: 'hard', render: h => h ? t('b4.block') : t('b4.alertOnly') },
      { title: '', render: (_: any, b: any) => can('budget.manage') && <Button size="small" danger onClick={async () => { await api.delete(`/budgets/${b.id}`); load() }}>{t('b4.del')}</Button> }]} />
    <Modal open={open} title={t('b4.addBudget')} onCancel={() => setOpen(false)} onOk={async () => { await api.post('/budgets', await form.validateFields()); setOpen(false); load(); message.success(t('b1.saved')) }}>
      <Form form={form} layout="vertical" initialValues={{ scope: 'user', alert_pct: 80, hard: true }}>
        <Form.Item name="scope" label={t('b4.scope')}><Select options={['tenant', 'department', 'user'].map(s => ({ value: s, label: t('b4.sc_' + s) }))} /></Form.Item>
        {scope === 'user' && <Form.Item name="scope_id" label={t('b4.target')} rules={[{ required: true }]}><RemoteSelect url="/users" /></Form.Item>}
        {scope === 'department' && <Form.Item name="scope_id" label={t('b4.target')} rules={[{ required: true }]}><Select options={deps.map(d => ({ value: d.id, label: d.name }))} /></Form.Item>}
        <Form.Item name="monthly_limit" label={t('b4.limit')} rules={[{ required: true }]}><InputNumber min={1} /></Form.Item>
        <Form.Item name="alert_pct" label={t('b4.alertPct')}><InputNumber min={1} max={100} /></Form.Item>
        <Form.Item name="hard" label={t('b4.hard')} valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <div style={{ color: '#888', marginTop: 8 }}>{t('b4.budgetLevels')}</div>
  </>
}

function Estimate() {
  const { t } = useTranslation()
  const [a, setA] = useState('EMAIL_FIND')
  const [q, setQ] = useState(100)
  const [r, setR] = useState<any>()
  return <Space direction="vertical">
    <Space><Select value={a} onChange={setA} style={{ width: 200 }} options={['EMAIL_FIND', 'EMAIL_VERIFY', 'RESEARCH', 'AI_WRITE'].map(x => ({ value: x, label: t('b4.act_' + x) }))} />
      <InputNumber min={1} max={100000} value={q} onChange={v => setQ(v || 1)} /><Button onClick={async () => setR((await api.post('/billing/estimate', { action: a, qty: q })).data)}>{t('b4.estimate')}</Button></Space>
    {r && <Descriptions bordered size="small" column={4} items={[{ key: 'l', label: t('b4.low'), children: r.low }, { key: 'm', label: t('b4.mid'), children: r.mid },
      { key: 'h', label: t('b4.high'), children: r.high }, { key: 'b', label: t('b4.balance'), children: <>{r.balance} {r.enough ? <Tag color="green">{t('b4.enough')}</Tag> : <Tag color="red">{t('b4.notEnough')}</Tag>}</> }]} />}
    {r && <div style={{ color: '#888' }}>{r.note}</div>}
  </Space>
}

function Usage() {
  const { t } = useTranslation()
  const [days, setDays] = useState(30)
  const [u, setU] = useState<any>()
  useEffect(() => { api.get('/billing/usage', { params: { days } }).then(r => setU(r.data)) }, [days])
  if (!u) return null
  return <>
    <Space style={{ marginBottom: 12 }}><Select value={days} onChange={setDays} options={[7, 30, 90, 365].map(d => ({ value: d, label: t('b4.lastDays', { n: d }) }))} /><Statistic title={t('b4.total')} value={u.total} /></Space>
    <ResponsiveContainer width="100%" height={220}><BarChart data={u.by_day}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="date" /><YAxis /><Tooltip /><Bar dataKey="credits" name={t('b4.credits')} fill="#1677ff" /></BarChart></ResponsiveContainer>
    <Row gutter={12}><Col span={14}><Table rowKey="user_id" size="small" pagination={false} dataSource={u.by_user} columns={[{ title: t('b4.member'), dataIndex: 'name' },
      { title: t('b4.credits'), dataIndex: 'credits' }, { title: t('b4.qualified'), dataIndex: 'qualified' }, { title: t('b4.perQualified'), dataIndex: 'per_qualified', render: v => v ?? '—' }]} /></Col>
      <Col span={10}><Table rowKey="k" size="small" pagination={false} dataSource={Object.entries(u.by_action).map(([k, v]) => ({ k, v }))} columns={[{ title: t('b4.action'), dataIndex: 'k', render: k => t('b4.act_' + k, { defaultValue: k }) }, { title: t('b4.credits'), dataIndex: 'v' }]} /></Col></Row>
  </>
}

function Bills() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [notices, setNotices] = useState<any[]>([])
  const load = () => { api.get('/billing/invoices').then(r => setRows(r.data.items || r.data)); api.get('/billing/price-notices').then(r => setNotices(r.data)) }
  useEffect(() => { load() }, [])
  const pay = async (no: string) => {
    try { const r = await api.post(`/billing/invoices/${no}:checkout`)
      Modal.confirm({ title: t('b4.mockPay'), content: t('b4.mockPayNote'), onOk: async () => { await api.post(`/billing/mock-pay/${r.data.pay_url.split('/').pop().split('?')[0]}`, { sig: r.data.sig }); message.success(t('b4.paid')); load() } })
    } catch { /* 错误提示由拦截器处理 */ }
  }
  return <>
    {notices.map(n => <Alert key={n.title + n.effective_on} type="warning" showIcon style={{ marginBottom: 8 }} message={`${n.title}（${n.effective_on} ${t('b4.effective')}）`} description={n.body} />)}
    <Table rowKey="invoice_no" size="small" dataSource={rows} columns={[{ title: t('b4.invoiceNo'), dataIndex: 'invoice_no' }, { title: t('b4.period'), dataIndex: 'period' },
      { title: t('b4.amount'), render: (_: any, r: any) => `${(r.amount_minor / 100).toFixed(2)} ${r.currency}` }, { title: t('b4.desc'), dataIndex: 'description' },
      { title: t('b4.status'), dataIndex: 'status', render: s => <Tag color={s === 'paid' ? 'green' : s === 'void' ? 'default' : 'orange'}>{t('b4.is_' + s, { defaultValue: s })}</Tag> },
      { title: '', render: (_: any, r: any) => r.status === 'issued' && can('billing.manage') && <Button size="small" onClick={() => pay(r.invoice_no)}>{t('b4.payOnline')}</Button> }]} />
  </>
}

export default function BillingPlusPage() {
  const { t } = useTranslation()
  return <Card title={t('menu.billingPlus')}><Tabs items={[
    { key: 'p', label: t('b4.planSeats'), children: <Plan /> }, { key: 'b', label: t('b4.budgets'), children: <Budgets /> },
    { key: 'e', label: t('b4.estimate'), children: <Estimate /> }, { key: 'u', label: t('b4.usage'), children: <Usage /> },
    { key: 'i', label: t('b4.bills'), children: <Bills /> }]} /></Card>
}
