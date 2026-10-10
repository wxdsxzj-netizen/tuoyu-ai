/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Input, InputNumber, List, Modal, Popover, Row, Select, Space, Table, Tag, Typography, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api, TOKEN_KEY } from '../api/client'
import { useAuth } from '../auth'

function target(v: string) {
  v = v.trim()
  if (v.includes('@')) return { email: v }
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(v.replace(/^https?:\/\//, '').replace(/\/.*$/, ''))) return { domain: v.replace(/^https?:\/\//, '').replace(/\/.*$/, '') }
  return { name: v }
}

function RiskPanel({ r, onDone }: { r: any, onDone: () => void }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const act = (idx: number, action: string) => {
    let note = ''
    Modal.confirm({ title: t('b3.risk_' + action), content: <Input.TextArea placeholder={t('b3.riskNote')} onChange={e => { note = e.target.value }} />,
      onOk: async () => { await api.post(`/research/${r.id}/risks/${idx}`, { action, note }); message.success(t('b1.done')); onDone() } })
  }
  if (!(r.risks || []).length) return null
  return <Card size="small" title={t('b3.riskHandling')} style={{ marginBottom: 12 }}>
    {r.risks.map((x: any, i: number) => { const done = r.risk_actions?.[String(i)]
      return <div key={i} style={{ marginBottom: 6 }}><Tag color={x.level === 'high' ? 'red' : 'orange'}>{x.level || 'risk'}</Tag>{x.text}{' '}
        {done ? <Tag color="green">{t('b3.risk_' + done.action)}{done.note ? '：' + done.note : ''}</Tag> : <Space size={4}>
          {['accepted', 'ignored', 'escalated'].map(a => <Button key={a} size="small" onClick={() => act(i, a)}>{t('b3.risk_' + a)}</Button>)}
          {can('compliance.manage') && <Button size="small" danger onClick={() => act(i, 'blocked')}>{t('b3.risk_blocked')}</Button>}</Space>}</div> })}
  </Card>
}

async function downloadPdf(id: string) {
  const res = await fetch(`/api/v1/research/${id}/pdf`, { headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` } })
  const url = URL.createObjectURL(await res.blob()); const a = document.createElement('a'); a.href = url; a.download = 'research.pdf'; a.click(); URL.revokeObjectURL(url)
}

function ShareBtn({ id }: { id: string }) {
  const { t } = useTranslation()
  const [days, setDays] = useState(7)
  return <Space.Compact>
    <InputNumber min={1} max={30} value={days} onChange={v => setDays(v || 7)} addonAfter={t('b3.days')} style={{ width: 110 }} />
    <Button onClick={async () => { const r = await api.post(`/research/${id}:share`, { days }); const link = location.origin + r.data.url
      Modal.success({ title: t('b3.shareLink'), content: <Typography.Paragraph copyable>{link}</Typography.Paragraph> }) }}>{t('b3.share')}</Button>
    <Button onClick={async () => { const r = await api.post(`/research/${id}/shares:revoke`); message.success(t('b3.revoked', { n: r.data.revoked })) }}>{t('b3.revokeShare')}</Button>
  </Space.Compact>
}

function Report({ r, onReload }: { r: any, onReload?: () => void }) {
  const { t } = useTranslation()
  const ev: Record<string, any> = Object.fromEntries((r.evidence || []).map((e: any) => [e.id, e]))
  return <Card title={<Space>{r.company_name}<Tag>{r.domain}</Tag><Tag>{t('p5.version')} {r.version}</Tag>{r.cached && <Tag color="green">{t('p5.cached')}</Tag>}
    {r.expired && <Tag color="orange">{t('b3.expired')}</Tag>}</Space>} extra={<Space><Button size="small" onClick={() => downloadPdf(r.id)}>PDF</Button><ShareBtn id={r.id} /><span>{t('p5.charged')} {r.credits_charged}</span><span>{t('p5.fit')} {r.fit_score}</span></Space>}>
    {r.status === 'insufficient' && <Alert type="warning" showIcon message={t('p5.insufficient')} style={{ marginBottom: 12 }} />}
    {r.expired && <Alert type="warning" showIcon message={t('b3.expiredHint')} style={{ marginBottom: 8 }} />}
    <RiskPanel r={r} onDone={async () => { onReload?.() }} />
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

function InquiryPick({ onPick }: { onPick: (id: string) => void }) {
  const { t } = useTranslation()
  const [opts, setOpts] = useState<any[]>([])
  useEffect(() => { api.get('/inquiries', { params: { size: 50 } }).then(r => setOpts((r.data.items || []).map((i: any) => ({ value: i.id, label: `${i.grade || '-'} · ${i.peer_email}` })))).catch(() => setOpts([])) }, [])
  return <Select style={{ width: 280 }} placeholder={t('b3.fromInquiry')} options={opts} onChange={onPick} showSearch optionFilterProp="label" />
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
      <Space style={{ marginBottom: 12 }}>
        <Upload accept=".png,.jpg,.jpeg" showUploadList={false} customRequest={async ({ file, onSuccess }: any) => {
          setLoading(true)
          try { const fd = new FormData(); fd.append('file', file); setRep((await api.post('/research:from-card', fd)).data); loadHist(); onSuccess?.({}) } finally { setLoading(false) }
        }}><Button loading={loading}>{t('b3.fromCard')}</Button></Upload>
        <InquiryPick onPick={async id => { setLoading(true); try { setRep((await api.post(`/research:from-inquiry/${id}`)).data); loadHist() } finally { setLoading(false) } }} />
      </Space>
      {rep && <Report r={rep} onReload={async () => setRep((await api.get(`/research/${rep.id}`)).data)} />}
    </Col>
    <Col span={7}><Card size="small" title={t('p5.history')}><Table size="small" rowKey="id" dataSource={hist} pagination={{ pageSize: 15 }}
      onRow={r => ({ onClick: async () => setRep((await api.get(`/research/${r.id}`)).data), style: { cursor: 'pointer' } })} columns={[
        { title: '公司', dataIndex: 'company_name' }, { title: 'v', dataIndex: 'version', width: 40 },
        { title: '', dataIndex: 'status', render: s => s === 'ok' ? <Tag color="green">完整</Tag> : <Tag color="orange">信息不足</Tag> }]} /></Card></Col>
  </Row>
}
