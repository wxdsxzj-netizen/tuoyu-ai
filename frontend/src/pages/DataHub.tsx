/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Input, List, Space, Table, Tabs, Tag, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAuth } from '../auth'

function Hub() {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [items, setItems] = useState<any[]>([])
  const [cur, setCur] = useState<any>()
  const load = (qq = q) => api.get('/hub/companies', { params: { q: qq || undefined } }).then(r => setItems(r.data.items))
  useEffect(() => { load() }, [])
  const open = (gid: string) => api.get(`/hub/companies/${gid}`).then(r => setCur(r.data))
  const unlock = (gid: string) => api.post('/hub:unlock', { entity_type: 'company', gid }).then(() => { message.success(t('b6.unlocked')); open(gid); load() })
  return <>
    <Space.Compact style={{ width: '100%', marginBottom: 12 }}><Input value={q} onChange={e => setQ(e.target.value)} onPressEnter={() => load()} placeholder={t('b6.searchHub')} /><Button type="primary" onClick={() => load()}>{t('b6.search')}</Button></Space.Compact>
    <Table size="small" rowKey="gid" dataSource={items} onRow={r => ({ onClick: () => open(r.gid) })} columns={[
      { title: t('b6.name'), dataIndex: 'name' }, { title: t('b6.domain'), dataIndex: 'domain' }, { title: t('b6.country'), dataIndex: 'country' },
      { title: t('b6.freshness'), dataIndex: 'freshness', render: (v: string) => <Tag color={v === 'fresh' ? 'green' : v === 'aging' ? 'orange' : 'red'}>{t(`b6.fr.${v}`)}</Tag> },
      { title: t('b6.confidence'), dataIndex: 'confidence' }, { title: '', render: (_: any, r: any) => r.unlocked ? <Tag color="blue">{t('b6.unlocked')}</Tag> : <Button size="small" onClick={e => { e.stopPropagation(); unlock(r.gid) }}>{t('b6.unlock')} 1{t('b6.credit')}</Button> }]} />
    {cur && <Card size="small" style={{ marginTop: 12 }} title={cur.name} extra={!cur.unlocked && <Button type="primary" onClick={() => unlock(cur.gid)}>{t('b6.unlock')}</Button>}>
      <Descriptions size="small" column={3} items={[{ key: 'd', label: t('b6.domain'), children: cur.domain }, { key: 'c', label: t('b6.country'), children: cur.country },
        { key: 'i', label: t('b6.industry'), children: cur.industry }, { key: 'f', label: t('b6.freshness'), children: cur.freshness },
        { key: 'cf', label: t('b6.confidence'), children: cur.confidence }, { key: 'hs', label: 'HS', children: (cur.hs_codes || []).join(', ') }]} />
      <div style={{ marginTop: 8 }}>{cur.description}</div>
      {cur.provenance && <Alert style={{ marginTop: 8 }} type="info" message={t('b6.provenance')} description={Object.entries(cur.provenance).map(([k, v]: any) => `${k}: ${v.source} · ${v.confidence} · ${v.fetched_at?.slice(0, 10)}`).join('；')} />}
      {cur.locked_fields && <Alert type="warning" message={t('b6.lockedHint')} />}
    </Card>}
  </>
}

function Customs() {
  const { t } = useTranslation()
  const [domain, setDomain] = useState('kraft-hvac.de')
  const [hs, setHs] = useState('8481')
  const [chain, setChain] = useState<any>()
  const [peers, setPeers] = useState<any>()
  const [cycle, setCycle] = useState<any>()
  const [mkt, setMkt] = useState<any>()
  const run = () => {
    api.get('/customs/supply-chain', { params: { domain } }).then(r => setChain(r.data))
    api.get('/customs/buy-cycle', { params: { domain } }).then(r => setCycle(r.data))
    api.get('/customs/supplier-switch', { params: { domain } }).then(r => setPeers(r.data))
    api.get('/customs/peers', { params: { exporter_domain: 'nbvalve.cn', hs } }).then(r => setPeers((p: any) => ({ ...p, rivals: r.data })))
    api.get('/customs/market', { params: { hs } }).then(r => setMkt(r.data))
  }
  return <>
    <Space style={{ marginBottom: 12 }}><Input value={domain} onChange={e => setDomain(e.target.value)} style={{ width: 200 }} placeholder="domain" />
      <Input value={hs} onChange={e => setHs(e.target.value)} style={{ width: 100 }} placeholder="HS" /><Button type="primary" onClick={run}>{t('b6.analyze')}</Button></Space>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b6.customsNote')} />
    {cycle && <Descriptions size="small" title={t('b6.buyCycle')} column={3} items={[{ key: 'm', label: t('b6.medianDays'), children: cycle.median_days ?? '—' },
      { key: 'n', label: t('b6.nextWindow'), children: cycle.next_window ?? '—' }, { key: 's', label: t('b6.shipments'), children: cycle.shipments }]} />}
    {peers?.switched != null && <Alert style={{ marginTop: 8 }} type={peers.switched ? 'warning' : 'success'} message={peers.switched ? t('b6.switched', { a: peers.previous_main, b: peers.current_main }) : t('b6.noSwitch')} />}
    {chain && <Table size="small" style={{ marginTop: 12 }} title={() => t('b6.suppliers')} rowKey="name" pagination={false} dataSource={chain.suppliers}
      columns={[{ title: t('b6.name'), dataIndex: 'name' }, { title: t('b6.shipments'), dataIndex: 'shipments' }, { title: 'USD', dataIndex: 'value_usd' }, { title: 'HS', dataIndex: 'hs_codes', render: (v: any) => (v || []).join(',') }]} />}
    {peers?.rivals && <Table size="small" style={{ marginTop: 12 }} title={() => t('b6.peers')} rowKey="exporter" pagination={false} dataSource={peers.rivals.rivals}
      columns={[{ title: t('b6.exporter'), dataIndex: 'exporter' }, { title: t('b6.sharedBuyers'), dataIndex: 'shared_buyers' }, { title: t('b6.shipments'), dataIndex: 'shipments' }]} />}
    {mkt && <>
      <ResponsiveContainer width="100%" height={220}><BarChart data={mkt.months}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis /><Tooltip /><Bar dataKey="value" fill="#1677ff" /></BarChart></ResponsiveContainer>
      <div>{t('b6.marketTotal')}: ${mkt.total_value} {mkt.growth_pct != null && <Tag>{mkt.growth_pct}%</Tag>}</div>
    </>}
  </>
}

function Signals() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const [sigs, setSigs] = useState<any[]>([])
  const [subs, setSubs] = useState<any[]>([])
  const [kinds, setKinds] = useState<any>({})
  const load = () => { api.get('/signals').then(r => { setSigs(r.data.items); setKinds(r.data.kinds) }); if (can('signal.manage')) api.get('/signal-subscriptions').then(r => setSubs(r.data)) }
  useEffect(() => { load() }, [])
  return <>
    {can('signal.manage') && <Card size="small" title={t('b6.subs')} style={{ marginBottom: 12 }} extra={<Button onClick={() => {
      const name = prompt(t('b6.subName')); if (!name) return
      api.post('/signal-subscriptions', { name, kinds: ['buy_window', 'supplier_switch', 'hiring'], countries: [] }).then(load)
    }}>{t('b6.newSub')}</Button>}>
      <List dataSource={subs} renderItem={(s: any) => <List.Item actions={[<a key="d" onClick={() => api.delete(`/signal-subscriptions/${s.id}`).then(load)}>{t('b6.delete')}</a>]}>{s.name} {(s.kinds || []).map((k: string) => <Tag key={k}>{kinds[k] || k}</Tag>)} · {s.hits} hits</List.Item>} />
    </Card>}
    <Table size="small" rowKey="id" dataSource={sigs} columns={[{ title: t('b6.kind'), dataIndex: 'kind', render: (v: string) => kinds[v] || v }, { title: t('b6.title'), dataIndex: 'title' },
      { title: t('b6.domain'), dataIndex: 'domain' }, { title: t('b6.country'), dataIndex: 'country' }, { title: t('b6.score'), dataIndex: 'score' }]} />
  </>
}

function Channels() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const [li, setLi] = useState<any[]>([])
  const [wa, setWa] = useState<any[]>([])
  const [leads, setLeads] = useState<any[]>([])
  const load = () => {
    if (can('channel.linkedin')) api.get('/linkedin/tasks').then(r => setLi(r.data))
    if (can('channel.wa')) api.get('/whatsapp/accounts').then(r => setWa(r.data))
    if (can('channel.alibaba')) api.get('/alibaba/leads').then(r => setLeads(r.data))
  }
  useEffect(() => { load() }, [])
  return <>
    <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t('b6.liNote')} />
    {can('channel.linkedin') && <Card size="small" title={t('b6.linkedin')} style={{ marginBottom: 12 }} extra={<Button onClick={() => {
      const url = prompt('LinkedIn URL'); if (!url) return
      api.post('/linkedin/tasks', { profile_url: url, kind: 'message', draft_text: 'Hi, ...' }).then(() => { message.success(t('b6.taskCreated')); load() })
    }}>{t('b6.newLi')}</Button>}>
      <Table size="small" rowKey="id" dataSource={li} columns={[{ title: t('b6.kind'), dataIndex: 'kind' }, { title: 'URL', dataIndex: 'profile_url', ellipsis: true },
        { title: t('b6.status'), dataIndex: 'status' }, { title: '', render: (_: any, r: any) => r.status === 'todo' && <a onClick={() => api.post(`/linkedin/tasks/${r.id}:complete`, { status: 'done' }).then(load)}>{t('b6.markDone')}</a> }]} />
    </Card>}
    {can('channel.wa') && <Card size="small" title="WhatsApp" style={{ marginBottom: 12 }} extra={<Button onClick={() => api.post('/whatsapp/accounts', { phone_number_id: 'mock', display_phone: '+10000000000' }).then(load)}>{t('b6.addWa')}</Button>}>
      <Alert type="info" message={t('b6.waNote')} style={{ marginBottom: 8 }} />
      <Table size="small" rowKey="id" dataSource={wa} columns={[{ title: t('b6.phone'), dataIndex: 'display_phone' }, { title: t('b6.status'), dataIndex: 'status' },
        { title: t('b6.sentToday'), render: (_: any, r: any) => `${r.sent_today}/${r.daily_limit}` }]} />
    </Card>}
    {can('channel.alibaba') && <Card size="small" title={t('b6.alibaba')}>
      <Upload accept=".csv" showUploadList={false} customRequest={({ file }: any) => { const fd = new FormData(); fd.append('file', file); api.post('/alibaba/import', fd).then(r => { message.success(JSON.stringify(r.data)); load() }) }}>
        <Button>{t('b6.importCsv')}</Button>
      </Upload>
      <Table size="small" style={{ marginTop: 8 }} rowKey="id" dataSource={leads} columns={[{ title: 'ID', dataIndex: 'external_id' }, { title: t('b6.email'), dataIndex: 'buyer_email' },
        { title: t('b6.company'), dataIndex: 'company' }, { title: t('b6.product'), dataIndex: 'product' }, { title: t('b6.status'), dataIndex: 'status' }]} />
    </Card>}
  </>
}

function Quality() {
  const { t } = useTranslation()
  const [d, setD] = useState<any>()
  const [changes, setChanges] = useState<any[]>([])
  useEffect(() => { api.get('/hub/quality').then(r => setD(r.data)); api.get('/contact-changes').then(r => setChanges(r.data)) }, [])
  if (!d) return null
  return <>
    <Table size="small" rowKey="country" dataSource={d.regions} columns={[{ title: t('b6.country'), dataIndex: 'country' }, { title: t('b6.companies'), dataIndex: 'companies' },
      { title: t('b6.fr.fresh'), dataIndex: 'fresh' }, { title: t('b6.fr.aging'), dataIndex: 'aging' }, { title: t('b6.fr.stale'), dataIndex: 'stale' },
      { title: t('b6.confidence'), dataIndex: 'avg_confidence' }, { title: t('b6.unlocks'), dataIndex: 'unlocks' }]} />
    <Card size="small" title={t('b6.contactChanges')} style={{ marginTop: 12 }}>
      <List dataSource={changes} locale={{ emptyText: t('b6.noChanges') }} renderItem={(x: any) => <List.Item actions={[<a key="a" onClick={() => api.post(`/contact-changes/${x.id}:ack`).then(() => setChanges(changes.filter(c => c.id !== x.id)))}>{t('b6.ack')}</a>]}>
        <Tag>{x.kind}</Tag>{x.before} → {x.after || '—'}</List.Item>} />
    </Card>
  </>
}

export default function DataHubPage() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const tabs = [
    can('hub.read') && { key: 'hub', label: t('b6.tabHub'), children: <Hub /> },
    can('hub.read') && { key: 'customs', label: t('b6.tabCustoms'), children: <Customs /> },
    can('hub.read') && { key: 'signals', label: t('b6.tabSignals'), children: <Signals /> },
    (can('channel.linkedin') || can('channel.wa') || can('channel.alibaba')) && { key: 'ch', label: t('b6.tabChannels'), children: <Channels /> },
    can('hub.read') && { key: 'q', label: t('b6.tabQuality'), children: <Quality /> },
  ].filter(Boolean) as any[]
  return <Card title={t('menu.dataHub')}><Tabs items={tabs} /></Card>
}
