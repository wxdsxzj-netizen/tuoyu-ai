/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Input, List, Popover, Row, Space, Table, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

function target(v: string) {
  v = v.trim()
  if (v.includes('@')) return { email: v }
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(v.replace(/^https?:\/\//, '').replace(/\/.*$/, ''))) return { domain: v.replace(/^https?:\/\//, '').replace(/\/.*$/, '') }
  return { name: v }
}

function Report({ r }: { r: any }) {
  const { t } = useTranslation()
  const ev: Record<string, any> = Object.fromEntries((r.evidence || []).map((e: any) => [e.id, e]))
  return <Card title={<Space>{r.company_name}<Tag>{r.domain}</Tag><Tag>{t('p5.version')} {r.version}</Tag>{r.cached && <Tag color="green">{t('p5.cached')}</Tag>}
    {r.expired && <Tag color="orange">已过期</Tag>}</Space>} extra={<Space><span>{t('p5.charged')} {r.credits_charged}</span><span>{t('p5.fit')} {r.fit_score}</span></Space>}>
    {r.status === 'insufficient' && <Alert type="warning" showIcon message={t('p5.insufficient')} style={{ marginBottom: 12 }} />}
    {(r.risks || []).filter((x: any) => x.level === 'high').map((x: any) => <Alert key={x.code} type="error" showIcon message={x.text} style={{ marginBottom: 8 }} />)}
    {r.sections.map((s: any) => <div key={s.key} style={{ marginBottom: 14 }}>
      <Typography.Title level={5} style={{ marginBottom: 4 }}>{s.title}</Typography.Title>
      {s.items.length === 0 ? <Typography.Text type="secondary">{t('p5.notFound')}</Typography.Text> :
        <ul style={{ margin: 0, paddingLeft: 20 }}>{s.items.map((it: any, i: number) => <li key={i}>{it.text}{' '}
          {it.inferred ? <Tag>{t('p5.inferred')}</Tag> : (it.evidence || []).map((e: string) => <Popover key={e} title={ev[e]?.title}
            content={<div style={{ maxWidth: 360 }}>{ev[e]?.snippet}{ev[e]?.url && <div><a href={ev[e].url} target="_blank" rel="noreferrer">{ev[e].url}</a></div>}</div>}>
            <Tag color="blue" style={{ cursor: 'pointer' }}>{e}</Tag></Popover>)}</li>)}</ul>}
    </div>)}
    <Card size="small" title={t('p5.evidence')}><List size="small" dataSource={r.evidence} renderItem={(e: any) => <List.Item>
      <Tag color="blue">{e.id}</Tag><Tag>{e.kind}</Tag>{e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}
      <Typography.Text type="secondary" style={{ marginLeft: 8 }}>{e.snippet}</Typography.Text></List.Item>} /></Card>
    {(r.robots_skipped || []).length > 0 && <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>{t('p5.robots')}：{r.robots_skipped.join('，')}</Typography.Paragraph>}
  </Card>
}

export default function ResearchPage() {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [rep, setRep] = useState<any>()
  const [hist, setHist] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const loadHist = () => api.get('/research').then(r => setHist(r.data))
  useEffect(() => { loadHist() }, [])
  const run = async (force = false, extra?: any) => {
    setLoading(true)
    try { setRep((await api.post('/research', { ...(extra ?? target(q)), force })).data); loadHist() } finally { setLoading(false) }
  }
  return <Row gutter={16}>
    <Col span={17}>
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input value={q} onChange={e => setQ(e.target.value)} placeholder={t('p5.target')} onPressEnter={() => run()} />
        <Button type="primary" loading={loading} disabled={!q.trim()} onClick={() => run()}>{t('p5.run')}</Button>
        {rep && <Button onClick={() => run(true, { company_id: rep.company_id })}>{t('p5.refresh')}</Button>}
      </Space.Compact>
      <Typography.Paragraph type="secondary">证据来源：数据中枢档案、联系人、公司官网（遵守 robots.txt，不抓内网地址）、制裁名单筛查、本租户往来邮件与询盘。事实带证据编号，推断单独标注。30 天内重复背调不扣费。</Typography.Paragraph>
      {rep && <Report r={rep} />}
    </Col>
    <Col span={7}><Card size="small" title={t('p5.history')}><Table size="small" rowKey="id" dataSource={hist} pagination={{ pageSize: 15 }}
      onRow={r => ({ onClick: async () => setRep((await api.get(`/research/${r.id}`)).data), style: { cursor: 'pointer' } })} columns={[
        { title: '公司', dataIndex: 'company_name' }, { title: 'v', dataIndex: 'version', width: 40 },
        { title: '', dataIndex: 'status', render: s => s === 'ok' ? <Tag color="green">完整</Tag> : <Tag color="orange">信息不足</Tag> }]} /></Card></Col>
  </Row>
}
