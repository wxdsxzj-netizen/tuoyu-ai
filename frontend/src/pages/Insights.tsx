/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Card, Col, Descriptions, Row, Select, Statistic, Table, Tabs, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAuth } from '../auth'

const fmt = (v: any, suf = '') => v == null ? '—' : `${v}${suf}`

function Brief() {
  const { t } = useTranslation()
  const [b, setB] = useState<any>()
  useEffect(() => { api.get('/analytics/morning-brief').then(r => setB(r.data)) }, [])
  if (!b) return null
  const y = b.yesterday
  return <>
    <Row gutter={12}>{['sent', 'replies', 'inquiries', 'handoffs', 'hard_bounces'].map(k => <Col span={4} key={k}><Card size="small"><Statistic title={t('b3.y_' + k)} value={y[k]} /></Card></Col>)}</Row>
    <div style={{ margin: '8px 0' }}>{t('b3.grades')}：{Object.entries(y.grades).map(([g, n]) => <Tag key={g}>{g} {String(n)}</Tag>)}</div>
    {b.highlights.map((h: string) => <Alert key={h} type="warning" showIcon message={h} style={{ marginBottom: 6 }} />)}
    <Descriptions size="small" bordered title={t('b3.todo')} column={3} items={Object.entries(b.todo).map(([k, v]) => ({ key: k, label: String(t('b3.td_' + k, { defaultValue: k })), children: String(v) }))} />
  </>
}

function Slices() {
  const { t } = useTranslation()
  const [dim, setDim] = useState('country')
  const [days, setDays] = useState(90)
  const [rows, setRows] = useState<any[]>([])
  useEffect(() => { api.get('/analytics/slices', { params: { dim, days } }).then(r => setRows(r.data)) }, [dim, days])
  return <>
    <Select value={dim} onChange={setDim} style={{ width: 140, marginRight: 8 }} options={['country', 'sequence', 'icp'].map(v => ({ value: v, label: t('b3.dim_' + v) }))} />
    <Select value={days} onChange={setDays} style={{ width: 120 }} options={[30, 90, 180, 365].map(v => ({ value: v, label: t('b3.lastDays', { n: v }) }))} />
    <div style={{ height: 280, marginTop: 8 }}><ResponsiveContainer><BarChart data={rows.slice(0, 15)}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="key" /><YAxis /><Tooltip /><Legend />
      {['enrolled', 'delivered', 'replied', 'accepted', 'won'].map((k, i) => <Bar key={k} dataKey={k} name={t('b3.f_' + k)} fill={['#8fb3ff', '#5b8ff9', '#5ad8a6', '#f6bd16', '#e8684a'][i]} />)}</BarChart></ResponsiveContainer></div>
    <Table rowKey="key" size="small" dataSource={rows} columns={[{ title: t('b3.dim_' + dim), dataIndex: 'key' },
      ...['enrolled', 'delivered', 'replied', 'accepted', 'won'].map(k => ({ title: t('b3.f_' + k), dataIndex: k })),
      { title: t('b3.replyRate'), dataIndex: 'reply_rate', render: (v: any) => fmt(v, '%') }, { title: t('b3.acceptRate'), dataIndex: 'accept_rate', render: (v: any) => fmt(v, '%') }]} />
  </>
}

function Unit() {
  const { t } = useTranslation()
  const [u, setU] = useState<any>()
  useEffect(() => { api.get('/analytics/unit-economics').then(r => setU(r.data)) }, [])
  if (!u) return null
  return <>
    <Row gutter={12}>{[['credits_spent', ''], ['emails_sent', ''], ['qualified_leads', ''], ['won_deals', ''], ['credits_per_qualified', ''], ['cost_per_qualified_cny', ' 元'], ['credits_per_won', ''], ['roi', 'x']].map(([k, s]) =>
      <Col span={6} key={k} style={{ marginBottom: 12 }}><Card size="small"><Statistic title={t('b3.u_' + k)} value={fmt(u[k], s)} /></Card></Col>)}</Row>
    <div>{t('b3.byAction')}：{Object.entries(u.by_action).map(([k, v]) => <Tag key={k}>{k} {String(v)}</Tag>)}</div>
    <div style={{ color: '#888', marginTop: 6 }}>{t('b3.creditCny', { v: u.credit_cny })}</div>
  </>
}

function Team() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  useEffect(() => { api.get('/analytics/team').then(r => setRows(r.data)) }, [])
  return <Table rowKey="user_id" size="small" dataSource={rows} columns={[{ title: t('b3.member'), dataIndex: 'name' }, { title: t('b3.role'), dataIndex: 'role' },
    ...['emails_sent', 'handoffs', 'accepted', 'inquiries', 'won', 'tasks_done'].map(k => ({ title: t('b3.t_' + k), dataIndex: k })),
    { title: t('b3.t_accept_rate'), dataIndex: 'accept_rate', render: (v: any) => fmt(v, '%') }, { title: t('b3.t_resp'), dataIndex: 'avg_first_response_h', render: (v: any) => fmt(v, 'h') },
    { title: t('b3.t_sla'), dataIndex: 'sla_rate', render: (v: any) => fmt(v, '%') }, { title: t('b3.t_won_amount'), dataIndex: 'won_amount' }]} />
}

function AiEff() {
  const { t } = useTranslation()
  const [a, setA] = useState<any>()
  useEffect(() => { api.get('/analytics/ai-efficacy').then(r => setA(r.data)) }, [])
  if (!a) return null
  return <><Row gutter={12}>{[['drafts', ''], ['ai_drafts', ''], ['approved', ''], ['approved_without_edit_rate', '%'], ['qa_veto_rate', '%'], ['avg_qa_score', ''], ['agent_tasks', ''], ['agent_completed', ''], ['hours_saved_est', 'h']].map(([k, s]) =>
    <Col span={6} key={k} style={{ marginBottom: 12 }}><Card size="small"><Statistic title={t('b3.a_' + k)} value={fmt(a[k], s)} /></Card></Col>)}</Row>
    <div style={{ color: '#888' }}>{a.assumption}</div></>
}

export default function InsightsPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  return <Tabs items={[
    { key: 'brief', label: t('b3.tabBrief'), children: <Brief /> },
    { key: 'slices', label: t('b3.tabSlices'), children: <Slices /> },
    ...(can('analytics.team') ? [{ key: 'unit', label: t('b3.tabUnit'), children: <Unit /> }, { key: 'team', label: t('b3.tabTeam'), children: <Team /> },
      { key: 'ai', label: t('b3.tabAi'), children: <AiEff /> }] : []),
  ]} />
}
