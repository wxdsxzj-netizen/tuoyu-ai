/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Card, Col, DatePicker, Row, Select, Space, Statistic, Table, Tabs, Tag, Typography } from 'antd'
import dayjs, { Dayjs } from 'dayjs'
import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`)
const COLORS = ['#cf1322', '#fa8c16', '#1677ff', '#8c8c8c']

function FunnelChart({ steps }: { steps: any[] }) {
  const { t } = useTranslation()
  return <>
    <ResponsiveContainer width="100%" height={Math.max(220, steps.length * 34)}>
      <BarChart data={steps} layout="vertical" margin={{ left: 30 }}>
        <CartesianGrid strokeDasharray="3 3" /><XAxis type="number" allowDecimals={false} /><YAxis type="category" dataKey="label" width={90} />
        <Tooltip formatter={(v: any) => [v, '数量']} /><Bar dataKey="value" fill="#1677ff" label={{ position: 'right' }} />
      </BarChart>
    </ResponsiveContainer>
    <Table size="small" rowKey="key" pagination={false} dataSource={steps} columns={[
      { title: '环节', dataIndex: 'label' }, { title: '数量', dataIndex: 'value' },
      { title: t('p5.conv'), dataIndex: 'rate', render: pct }, { title: t('p5.overall'), dataIndex: 'overall', render: pct }]} />
  </>
}

function Funnel() {
  const { t } = useTranslation()
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs().subtract(30, 'day'), dayjs()])
  const [owner, setOwner] = useState<string | undefined>()
  const [users, setUsers] = useState<any[]>([])
  const [d, setD] = useState<any>()
  const load = useCallback(async () => setD((await api.get('/analytics/funnel', { params: { since: range[0].toISOString(), until: range[1].toISOString(), owner_id: owner } })).data), [range, owner])
  useEffect(() => { load() }, [load])
  useEffect(() => { api.get('/users').then(r => setUsers(r.data.items ?? r.data)).catch(() => null) }, [])
  if (!d) return null
  const grades = Object.entries(d.inquiry.grades).map(([k, v]) => ({ name: `${k} 级`, value: v as number }))
  return <>
    <Space style={{ marginBottom: 12 }}>
      <DatePicker.RangePicker value={range} onChange={v => v && setRange(v as [Dayjs, Dayjs])} />
      {users.length > 0 && <Select allowClear placeholder="业务员" style={{ width: 160 }} value={owner} onChange={setOwner} options={users.map(u => ({ value: u.id, label: u.name }))} />}
      <Typography.Text type="secondary">{t('p5.asOf')} {new Date(d.as_of).toLocaleString()}</Typography.Text>
    </Space>
    <Row gutter={16}>
      <Col span={12}><Card size="small" title={t('p5.funnel')}><FunnelChart steps={d.acquisition} /></Card></Col>
      <Col span={12}><Card size="small" title={t('p5.inqFunnel')}><FunnelChart steps={d.inquiry.steps} />
        <Row style={{ marginTop: 12 }}>
          <Col span={12}><ResponsiveContainer width="100%" height={180}><PieChart><Pie data={grades} dataKey="value" nameKey="name" outerRadius={60} label>
            {grades.map((_, i) => <Cell key={i} fill={COLORS[i]} />)}</Pie><Tooltip /><Legend /></PieChart></ResponsiveContainer></Col>
          <Col span={12}><Statistic title={t('p5.avgResp')} value={d.inquiry.avg_first_response_min ?? '—'} />
            <Statistic title={t('p5.slaMet')} value={pct(d.inquiry.sla_met_rate)} /></Col>
        </Row></Card></Col>
    </Row>
  </>
}

function Deliverability() {
  const { t } = useTranslation()
  const [days, setDays] = useState(30)
  const [d, setD] = useState<any>()
  useEffect(() => { api.get('/analytics/deliverability', { params: { days } }).then(r => setD(r.data)) }, [days])
  if (!d) return null
  const series = d.series.map((s: any) => ({ ...s, bounce: s.bounce_rate === null ? null : +(s.bounce_rate * 100).toFixed(2),
    complaint: s.complaint_rate === null ? null : +(s.complaint_rate * 100).toFixed(3), reply: s.reply_rate === null ? null : +(s.reply_rate * 100).toFixed(2) }))
  return <>
    <Space style={{ marginBottom: 12 }}>
      <Select value={days} onChange={setDays} options={[7, 30, 90].map(x => ({ value: x, label: `近 ${x} 天` }))} />
      <Typography.Text type="secondary">{t('p5.asOf')} {new Date(d.as_of).toLocaleString()}</Typography.Text>
    </Space>
    <Row gutter={16} style={{ marginBottom: 12 }}>
      <Col span={5}><Card size="small"><Statistic title={t('p5.sent')} value={d.totals.sent} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.bounce')} value={pct(d.totals.bounce_rate)} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.complaint')} value={pct(d.totals.complaint_rate)} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.replyRate')} value={pct(d.totals.reply_rate)} /></Card></Col>
      <Col span={4}><Card size="small"><Statistic title={t('p5.placement')} value="—" /></Card></Col>
    </Row>
    <Row gutter={16}>
      <Col span={12}><Card size="small" title={t('p5.sent')}><ResponsiveContainer width="100%" height={240}><BarChart data={series}>
        <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="day" tickFormatter={v => v.slice(5)} /><YAxis allowDecimals={false} /><Tooltip />
        <Bar dataKey="sent" name={t('p5.sent')} fill="#1677ff" /></BarChart></ResponsiveContainer></Card></Col>
      <Col span={12}><Card size="small" title="退信 / 投诉 / 回复率（%）"><ResponsiveContainer width="100%" height={240}><LineChart data={series}>
        <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="day" tickFormatter={v => v.slice(5)} /><YAxis /><Tooltip /><Legend />
        <Line dataKey="bounce" name={t('p5.bounce')} stroke="#cf1322" connectNulls /><Line dataKey="complaint" name={t('p5.complaint')} stroke="#fa8c16" connectNulls />
        <Line dataKey="reply" name={t('p5.replyRate')} stroke="#389e0d" connectNulls /></LineChart></ResponsiveContainer></Card></Col>
    </Row>
    <Card size="small" title={t('p5.mailbox')} style={{ marginTop: 12 }}><Table size="small" rowKey="id" pagination={false} dataSource={d.mailboxes} columns={[
      { title: t('p5.mailbox'), dataIndex: 'address' }, { title: t('p5.health'), dataIndex: 'health_score' },
      { title: t('p5.level'), dataIndex: 'health_level', render: v => <Tag color={v === 'normal' ? 'green' : v === 'watch' ? 'gold' : 'red'}>{v}</Tag> },
      { title: t('p5.sent'), dataIndex: 'sent' }, { title: t('p5.bounce'), dataIndex: 'bounce_rate', render: pct },
      { title: t('p5.complaint'), dataIndex: 'complaint_rate', render: pct }, { title: t('p5.replyRate'), dataIndex: 'reply_rate', render: pct }]} /></Card>
    <Row gutter={16} style={{ marginTop: 12 }}>
      <Col span={12}><Card size="small" title={t('p5.domains')}><Table size="small" rowKey="id" pagination={false} dataSource={d.domains} columns={[
        { title: '域名', dataIndex: 'domain' }, ...['spf', 'dkim', 'dmarc', 'mx'].map(k => ({ title: k.toUpperCase(), dataIndex: k,
          render: (v: string) => <Tag color={v === 'pass' || v === 'ok' ? 'green' : v === 'unknown' ? 'default' : 'red'}>{v}</Tag> }))]} /></Card></Col>
      <Col span={12}><Card size="small" title={t('p5.breakers')}><Table size="small" rowKey={(r: any) => r.at + r.scope_id} pagination={false} dataSource={d.breaker_events} columns={[
        { title: '时间', dataIndex: 'at', render: v => new Date(v).toLocaleString() }, { title: '范围', dataIndex: 'scope' },
        { title: '级别', dataIndex: 'level' }, { title: '指标', render: (_, r: any) => `${r.metric} = ${r.value}` }]} /></Card></Col>
    </Row>
  </>
}

export default function AnalyticsPage() {
  const { t } = useTranslation()
  return <Tabs items={[{ key: 'f', label: t('p5.funnel'), children: <Funnel /> }, { key: 'd', label: t('p5.deliver'), children: <Deliverability /> }]} />
}
