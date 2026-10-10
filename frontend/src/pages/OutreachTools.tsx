/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Collapse, Input, InputNumber, Row, Select, Space, Table, Tabs, Tag, Timeline, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth'

function Templates() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [rows, setRows] = useState<any[]>([])
  useEffect(() => { api.get('/sequence-templates').then(r => setRows(r.data)) }, [])
  return <Row gutter={[12, 12]}>{rows.map(r => <Col span={6} key={r.key}><Card size="small" title={r.name} extra={<Tag>{r.steps} {t('b3.steps')}</Tag>}>
    <div style={{ minHeight: 40, color: '#666' }}>{r.description}</div>
    <Button size="small" type="primary" onClick={async () => { await api.post('/sequences:from-template', { template_key: r.key }); message.success(t('b3.tplCreated')); nav('/sequences') }}>{t('b3.useTpl')}</Button>
  </Card></Col>)}</Row>
}

function AbStats() {
  const { t } = useTranslation()
  const [seqs, setSeqs] = useState<any[]>([])
  const [rows, setRows] = useState<any[]>()
  useEffect(() => { api.get('/sequences').then(r => setSeqs(r.data.map((s: any) => ({ value: s.id, label: s.name })))) }, [])
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 8 }} message={t('b3.abHint')} />
    <Select style={{ width: 300, marginBottom: 8 }} options={seqs} placeholder={t('b3.pickSeq')} onChange={async v => setRows((await api.get(`/sequences/${v}/ab-stats`)).data)} />
    {rows && <Table rowKey={r => `${r.step_no}-${r.variant}`} size="small" dataSource={rows} columns={[
      { title: t('b3.step'), dataIndex: 'step_no' }, { title: t('b3.variant'), dataIndex: 'variant', render: (v, r: any) => <>{v} {r.winner && <Tag color="gold">{t('b3.winner')}</Tag>}</> },
      { title: t('b3.sent'), dataIndex: 'sent' }, { title: t('b3.replied'), dataIndex: 'replied' }, { title: t('b3.replyRate'), dataIndex: 'reply_rate', render: v => v == null ? '—' : v + '%' }]} />}
  </>
}

function Domains() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [main, setMain] = useState('')
  const [target, setTarget] = useState(200)
  const [plan, setPlan] = useState<any>()
  const [doms, setDoms] = useState<any[]>([])
  const [info, setInfo] = useState<Record<string, any>>({})
  const [guides, setGuides] = useState<any>({})
  useEffect(() => { api.get('/domains').then(r => setDoms(Array.isArray(r.data) ? r.data : r.data.items || [])); api.get('/dns-guides').then(r => setGuides(r.data)) }, [])
  const put = (id: string, k: string, v: any) => setInfo(x => ({ ...x, [id]: { ...(x[id] || {}), [k]: v } }))
  return <>
    <Card size="small" title={t('b3.domainPlan')} style={{ marginBottom: 12 }}>
      <Space><Input placeholder={t('b3.mainDomain')} value={main} onChange={e => setMain(e.target.value)} />{t('b3.dailyTarget')}<InputNumber min={1} value={target} onChange={v => setTarget(v || 1)} />
        <Button type="primary" disabled={main.length < 3} onClick={async () => setPlan((await api.post('/domains:plan', { main_domain: main, daily_target: target })).data)}>{t('b3.plan')}</Button></Space>
      {plan && <div style={{ marginTop: 8 }}>
        <Space><Tag color="blue">{t('b3.mbNeeded', { n: plan.mailboxes_needed })}</Tag><Tag color="blue">{t('b3.domNeeded', { n: plan.domains_needed })}</Tag><Tag>{t('b3.backup', { n: plan.backup_domains })}</Tag><Tag>{t('b3.warmWeeks', { n: plan.warmup_weeks })}</Tag></Space>
        <div style={{ marginTop: 6 }}>{t('b3.candidates')}：{plan.candidates.map((c: string) => <Tag key={c}>{c}</Tag>)}</div>
        <ul>{plan.advice.map((a: string) => <li key={a}>{a}</li>)}</ul></div>}
    </Card>
    <Table rowKey="id" size="small" dataSource={doms} pagination={false} columns={[
      { title: t('b3.domain'), dataIndex: 'domain', render: (v, r: any) => <>{v} {r.paused && <Tag color="red">{t('b3.paused')}</Tag>}</> },
      { title: t('b3.blacklist'), render: (_: any, r: any) => { const x = info[r.id]?.bl
        return <Space>{x && (x.listed_on.length ? x.listed_on.map((z: string) => <Tag color="red" key={z}>{z}</Tag>) : <Tag color="green">{t('b3.clean')}</Tag>)}
          {x && x.provider === 'mock' && <Tag>{t('b3.mockCheck')}</Tag>}
          {can('mailbox.manage') && <Button size="small" onClick={async () => put(r.id, 'bl', (await api.post(`/domains/${r.id}:blacklist-check`)).data)}>{t('b3.check')}</Button>}</Space> } },
      { title: t('b3.trackCname'), render: (_: any, r: any) => { const x = info[r.id]?.cn
        return <Space>{x && <Tag color={x.status === 'ok' ? 'green' : 'orange'}>{x.name} → {x.expected}：{t('b3.cn_' + x.status)}</Tag>}
          <Button size="small" onClick={async () => put(r.id, 'cn', (await api.get(`/domains/${r.id}/tracking-cname`)).data)}>{t('b3.check')}</Button></Space> } },
    ]} />
    <Card size="small" title={t('b3.guides')} style={{ marginTop: 12 }}>
      <Collapse items={Object.entries(guides).map(([k, v]: any) => ({ key: k, label: t('b3.p_' + k), children: <ol>{v.map((s: string) => <li key={s}>{s}</li>)}</ol> }))} />
    </Card>
  </>
}

function MessageTimeline() {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<any[]>([])
  const [tl, setTl] = useState<any>()
  const load = () => api.get('/outbound', { params: { q: q || undefined } }).then(r => setRows(r.data))
  useEffect(() => { load() }, [])
  return <Row gutter={12}><Col span={13}>
    <Input.Search placeholder={t('b3.searchMail')} value={q} onChange={e => setQ(e.target.value)} onSearch={load} style={{ marginBottom: 8 }} />
    <Table rowKey="id" size="small" dataSource={rows} onRow={r => ({ onClick: async () => setTl((await api.get(`/outbound/${r.id}/timeline`)).data), style: { cursor: 'pointer' } })}
      columns={[{ title: t('b3.to'), dataIndex: 'to' }, { title: t('b3.subject'), dataIndex: 'subject' }, { title: t('b3.status'), dataIndex: 'status' },
        { title: t('b3.sentAt'), dataIndex: 'sent_at', render: v => v ? new Date(v).toLocaleString() : '—' }]} /></Col>
    <Col span={11}>{tl && <Card size="small" title={`${tl.to} · ${tl.subject}`}><Timeline items={tl.events.map((e: any) => ({
      color: e.type.includes('bounce') || e.type === 'gate_blocked' ? 'red' : e.type.startsWith('reply') ? 'green' : 'blue',
      children: <><b>{String(t('b3.ev_' + e.type.split(':')[0], { defaultValue: e.type }))}</b> {e.reason && <Tag>{e.reason}</Tag>}<div style={{ color: '#888' }}>{new Date(e.at).toLocaleString()}</div></> }))} /></Card>}</Col>
  </Row>
}

export default function OutreachToolsPage() {
  const { t } = useTranslation()
  return <Tabs items={[
    { key: 'tpl', label: t('b3.tabTemplates'), children: <Templates /> },
    { key: 'ab', label: t('b3.tabAb'), children: <AbStats /> },
    { key: 'dom', label: t('b3.tabDomains'), children: <Domains /> },
    { key: 'tl', label: t('b3.tabTimeline'), children: <MessageTimeline /> },
  ]} />
}
