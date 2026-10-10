/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

function useOpts(url: string, label = 'name') {
  const [o, setO] = useState<any[]>([])
  useEffect(() => { api.get(url).then(r => setO((Array.isArray(r.data) ? r.data : r.data.items || []).map((x: any) => ({ value: x.id, label: x[label] })))).catch(() => setO([])) }, [url, label])
  return o
}

function SaveSearch({ query }: { query: any }) {
  const { t } = useTranslation()
  return <Button onClick={() => { let name = ''; let sub = true
    Modal.confirm({ title: t('b3.saveSearch'), content: <Space direction="vertical"><Input placeholder={t('b3.name')} onChange={e => { name = e.target.value }} />
      <Space><Switch defaultChecked onChange={v => { sub = v }} />{t('b3.subscribe')}</Space></Space>,
      onOk: async () => { await api.post('/saved-searches', { name: name || t('b3.saveSearch'), query, subscribed: sub }); message.success(t('b1.saved')) } }) }}>{t('b3.saveSearch')}</Button>
}

function CompanyTable({ rows, extra }: { rows: any[], extra?: any[] }) {
  const { t } = useTranslation()
  const [look, setLook] = useState<any>()
  return <>
    <Table rowKey="company_id" size="small" dataSource={rows} pagination={{ pageSize: 20 }} columns={[
      { title: t('b3.company'), dataIndex: 'name', render: (v, r: any) => <>{v} {r.excluded && <Tag color="red">{t('b3.excluded')}：{r.excluded}</Tag>}</> },
      { title: t('b3.domain'), dataIndex: 'domain' }, { title: t('b3.country'), dataIndex: 'country' },
      ...(extra || []),
      { title: '', render: (_: any, r: any) => <Button size="small" onClick={async () => setLook({ name: r.name, rows: (await api.get(`/companies/${r.company_id}/lookalike`)).data })}>{t('b3.lookalike')}</Button> },
    ]} />
    <Modal open={!!look} onCancel={() => setLook(undefined)} footer={null} width={760} title={`${t('b3.lookalike')}：${look?.name}`}>
      <Table rowKey="company_id" size="small" dataSource={look?.rows} columns={[{ title: t('b3.company'), dataIndex: 'name' }, { title: t('b3.country'), dataIndex: 'country' },
        { title: t('b3.similarity'), dataIndex: 'similarity' }, { title: t('b3.reasons'), dataIndex: 'reasons', render: v => v.map((x: string) => <Tag key={x}>{x}</Tag>) }]} />
    </Modal>
  </>
}

function IcpDiscover() {
  const { t } = useTranslation()
  const icps = useOpts('/icps')
  const [icp, setIcp] = useState<string>()
  const [inc, setInc] = useState(false)
  const [res, setRes] = useState<any>()
  const run = async () => icp && setRes((await api.get(`/discover/icp/${icp}`, { params: { include_excluded: inc, limit: 200 } })).data)
  return <>
    <Space style={{ marginBottom: 8 }}><Select style={{ width: 260 }} options={icps} value={icp} onChange={setIcp} placeholder={t('b3.pickIcp')} />
      <Switch checked={inc} onChange={setInc} />{t('b3.showExcluded')}<Button type="primary" disabled={!icp} onClick={run}>{t('b3.discover')}</Button>
      {icp && <SaveSearch query={{ icp_id: icp }} />}
      {icp && <Button onClick={async () => { await api.post('/lists', { name: t('b3.icpList'), kind: 'dynamic', object_type: 'company', filters: { icp_id: icp } }); message.success(t('b1.saved')) }}>{t('b3.toDynList')}</Button>}</Space>
    {res && <Alert type="info" showIcon style={{ marginBottom: 8 }} message={t('b3.discoverSummary', { n: res.total, x: res.excluded_total })}
      description={Object.entries(res.excluded).map(([k, v]) => <Tag key={k}>{t('b3.rt_' + k)} {String(v)}</Tag>)} />}
    {res && <CompanyTable rows={res.items} extra={[
      { title: t('b3.score'), dataIndex: 'score', sorter: (a: any, b: any) => a.score - b.score },
      { title: t('b3.match'), dataIndex: 'match_score' }, { title: t('b3.intent'), dataIndex: 'intent_score' },
      { title: t('b3.reasons'), dataIndex: 'reasons', render: (v: string[]) => v.slice(0, 4).map(x => <Tag key={x}>{x}</Tag>) }]} />}
  </>
}

function Semantic() {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [res, setRes] = useState<any>()
  return <>
    <Space.Compact style={{ width: '100%', marginBottom: 8 }}><Input value={q} onChange={e => setQ(e.target.value)} placeholder={t('b3.semanticPh')} onPressEnter={async () => setRes((await api.get('/search/semantic', { params: { q } })).data)} />
      <Button type="primary" disabled={q.trim().length < 2} onClick={async () => setRes((await api.get('/search/semantic', { params: { q } })).data)}>{t('b3.search')}</Button>
      {q.trim().length > 1 && <SaveSearch query={{ text: q }} />}</Space.Compact>
    {res && <div style={{ marginBottom: 8 }}>{t('b3.parsed')}：{res.parsed.countries.map((c: string) => <Tag color="blue" key={c}>{c}</Tag>)}{res.parsed.terms.map((c: string) => <Tag key={c}>{c}</Tag>)}</div>}
    {res && <CompanyTable rows={res.items} extra={[{ title: t('b3.relevance'), dataIndex: 'relevance' }, { title: t('b3.matched'), dataIndex: 'matched', render: (v: string[]) => v.join(', ') }]} />}
  </>
}

function Lists() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [view, setView] = useState<any>()
  const [form] = Form.useForm()
  const seqs = useOpts('/sequences')
  const load = () => api.get('/lists').then(r => setRows(r.data))
  useEffect(() => { load() }, [])
  const create = async () => {
    const v = await form.validateFields()
    const filters = v.kind === 'dynamic' ? { seniority: v.seniority, countries: v.countries ? v.countries.split(/[,，\s]+/).filter(Boolean) : undefined, email_status: v.email_status } : {}
    await api.post('/lists', { name: v.name, kind: v.kind, filters }); form.resetFields(); load()
  }
  const enroll = (id: string) => { let sid = ''
    Modal.confirm({ title: t('b3.enrollList'), content: <Select style={{ width: '100%' }} options={seqs} onChange={v => { sid = v }} />,
      onOk: async () => { const r = await api.post(`/lists/${id}:enroll`, { sequence_id: sid }); message.success(String(t('b3.enrollRes', r.data as any))) } }) }
  return <>
    <Form form={form} layout="inline" initialValues={{ kind: 'static' }} style={{ marginBottom: 8 }}>
      <Form.Item name="name" rules={[{ required: true }]}><Input placeholder={t('b3.name')} /></Form.Item>
      <Form.Item name="kind"><Select style={{ width: 110 }} options={['static', 'dynamic'].map(v => ({ value: v, label: t('b3.k_' + v) }))} /></Form.Item>
      <Form.Item noStyle shouldUpdate>{() => form.getFieldValue('kind') === 'dynamic' && <>
        <Form.Item name="seniority"><Select mode="multiple" style={{ width: 200 }} placeholder={t('b3.seniority')} options={['c_level', 'vp', 'director', 'manager', 'staff'].map(v => ({ value: v, label: t('b3.s_' + v) }))} /></Form.Item>
        <Form.Item name="countries"><Input placeholder={t('b3.countriesPh')} style={{ width: 140 }} /></Form.Item>
        <Form.Item name="email_status"><Select mode="multiple" style={{ width: 160 }} placeholder={t('b3.emailStatus')} options={['valid', 'accept_all', 'unknown'].map(v => ({ value: v, label: v }))} /></Form.Item></>}</Form.Item>
      <Button type="primary" onClick={create}>{t('b3.createList')}</Button>
    </Form>
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('b3.name'), dataIndex: 'name' }, { title: t('b3.kind'), dataIndex: 'kind', render: v => <Tag>{t('b3.k_' + v)}</Tag> },
      { title: t('b3.objType'), dataIndex: 'object_type', render: v => t('b3.o_' + v) }, { title: t('b3.count'), dataIndex: 'count' },
      { title: '', render: (_: any, r: any) => <Space><Button size="small" onClick={async () => setView((await api.get(`/lists/${r.id}`)).data)}>{t('b3.view')}</Button>
        {r.object_type === 'contact' && <Button size="small" onClick={() => enroll(r.id)}>{t('b3.enrollList')}</Button>}
        <Button size="small" danger onClick={async () => { await api.delete(`/lists/${r.id}`); load() }}>{t('b3.delete')}</Button></Space> },
    ]} />
    <Modal open={!!view} onCancel={() => setView(undefined)} footer={null} width={760} title={view?.name}>
      <Table rowKey="id" size="small" dataSource={view?.members} columns={[{ title: t('b3.name'), dataIndex: 'name' }, { title: 'Email', dataIndex: 'email' }, { title: t('b3.title'), dataIndex: 'title' },
        { title: '', render: (_: any, m: any) => view?.kind === 'static' && <Button size="small" onClick={async () => { await api.post(`/lists/${view.id}/members`, { remove: [m.id] }); setView((await api.get(`/lists/${view.id}`)).data); load() }}>{t('b3.remove')}</Button> }]} />
    </Modal>
  </>
}

function Saved() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const load = () => api.get('/saved-searches').then(r => setRows(r.data))
  useEffect(() => { load() }, [])
  return <><Alert type="info" showIcon style={{ marginBottom: 8 }} message={t('b3.savedHint')} />
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('b3.name'), dataIndex: 'name' }, { title: t('b3.query'), dataIndex: 'query', render: v => v.text || 'ICP' },
      { title: t('b3.subscribe'), dataIndex: 'subscribed', render: v => v ? <Tag color="green">{t('b3.on')}</Tag> : '—' },
      { title: t('b3.lastRun'), dataIndex: 'last_run_at', render: v => v ? new Date(v).toLocaleString() : '—' },
      { title: '', render: (_: any, r: any) => <Space><Button size="small" onClick={async () => { const x = await api.post(`/saved-searches/${r.id}:run`); message.info(t('b3.newFound', { n: x.data.new.length })); load() }}>{t('b3.run')}</Button>
        <Button size="small" danger onClick={async () => { await api.delete(`/saved-searches/${r.id}`); load() }}>{t('b3.delete')}</Button></Space> }]} /></>
}

function Exclusions() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [form] = Form.useForm()
  const load = () => api.get('/exclusion-rules').then(r => setRows(r.data))
  useEffect(() => { load() }, [])
  const types = ['domain', 'competitor', 'company', 'country', 'keyword', 'existing_customer']
  return <>
    {can('compliance.manage') && <Form form={form} layout="inline" style={{ marginBottom: 8 }} initialValues={{ rule_type: 'competitor' }}>
      <Form.Item name="rule_type"><Select style={{ width: 140 }} options={types.map(v => ({ value: v, label: t('b3.rt_' + v) }))} /></Form.Item>
      <Form.Item name="value"><Input placeholder={t('b3.value')} /></Form.Item><Form.Item name="note"><Input placeholder={t('b3.note')} /></Form.Item>
      <Button type="primary" onClick={async () => { await api.post('/exclusion-rules', await form.validateFields()); form.resetFields(['value', 'note']); load() }}>{t('b3.add')}</Button></Form>}
    <Table rowKey="id" size="small" dataSource={rows} columns={[{ title: t('b3.type'), dataIndex: 'rule_type', render: v => t('b3.rt_' + v) }, { title: t('b3.value'), dataIndex: 'value' },
      { title: t('b3.note'), dataIndex: 'note' }, { title: '', render: (_: any, r: any) => can('compliance.manage') && <Button size="small" danger onClick={async () => { await api.delete(`/exclusion-rules/${r.id}`); load() }}>{t('b3.delete')}</Button> }]} />
  </>
}

function Dormant() {
  const { t } = useTranslation()
  const [days, setDays] = useState(180)
  const [res, setRes] = useState<any>()
  const seqs = useOpts('/sequences')
  const [sid, setSid] = useState<string>()
  return <>
    <Space style={{ marginBottom: 8 }}>{t('b3.dormantDays')}<InputNumber min={30} max={1095} value={days} onChange={v => setDays(v || 180)} />
      <Button onClick={async () => setRes((await api.get('/dormant-contacts', { params: { days } })).data)}>{t('b3.find')}</Button>
      <Select style={{ width: 240 }} options={seqs} value={sid} onChange={setSid} placeholder={t('b3.pickSeq')} />
      <Button type="primary" disabled={!sid || !res?.count} onClick={async () => { const r = await api.post('/dormant-contacts:revive', { sequence_id: sid, days }); message.success(String(t('b3.enrollRes', r.data as any))) }}>{t('b3.revive')}</Button></Space>
    {res && <Table rowKey="id" size="small" dataSource={res.items} title={() => t('b3.count') + '：' + res.count} columns={[{ title: t('b3.name'), dataIndex: 'name' }, { title: 'Email', dataIndex: 'email' }, { title: t('b3.status'), dataIndex: 'lead_status' }]} />}
  </>
}

export default function DiscoverPage() {
  const { t } = useTranslation()
  return <Tabs items={[
    { key: 'icp', label: t('b3.tabIcp'), children: <IcpDiscover /> },
    { key: 'sem', label: t('b3.tabSemantic'), children: <Semantic /> },
    { key: 'lists', label: t('b3.tabLists'), children: <Lists /> },
    { key: 'saved', label: t('b3.tabSaved'), children: <Saved /> },
    { key: 'ex', label: t('b3.tabExclusions'), children: <Exclusions /> },
    { key: 'dormant', label: t('b3.tabDormant'), children: <Dormant /> },
  ]} />
}
