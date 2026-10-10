/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Form, Input, InputNumber, List, Modal, Select, Space, Switch, Table, Tabs, Tag, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api/client'
import { useAuth } from '../auth'

function Result({ r }: { r: any }) {
  const { t } = useTranslation()
  if (!r) return null
  if (r.clarify) return <Alert type="info" showIcon message={r.clarify} description={(r.examples || []).join(' / ')} />
  return <Card size="small" title={r.title} extra={<Space><Tag>{r.scope}</Tag>{r.total != null && <Tag color="blue">{t('b5.total')} {r.total}</Tag>}</Space>}>
    {r.rows.length > 1 && <ResponsiveContainer width="100%" height={220}><BarChart data={r.rows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="key" /><YAxis /><Tooltip /><Bar dataKey="value" fill="#1677ff" /></BarChart></ResponsiveContainer>}
    <Table size="small" rowKey="key" pagination={false} dataSource={r.rows} columns={[{ title: t('b5.dim'), dataIndex: 'key' }, { title: `${t('b5.value')} ${r.unit || ''}`, dataIndex: 'value', render: (v: any) => v ?? '—' }]} />
    <div style={{ color: '#888', marginTop: 6 }}>{t('b5.asOf')} {r.as_of?.slice(0, 19)}</div>
  </Card>
}

function AskData() {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const [r, setR] = useState<any>()
  const [reps, setReps] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const load = () => api.get('/reports').then(x => setReps(x.data))
  useEffect(() => { load() }, [])
  const ask = (text: string) => api.post('/ask-data', { q: text }).then(x => setR(x.data))
  const metrics = ['sent', 'replies', 'reply_rate', 'inquiries', 'handoffs_accepted', 'won', 'revenue']
  const dims = ['none', 'country', 'owner', 'month', 'grade']
  return <>
    <Space.Compact style={{ width: '100%' }}><Input value={q} onChange={e => setQ(e.target.value)} onPressEnter={() => ask(q)} placeholder={t('b5.askPh')} /><Button type="primary" onClick={() => ask(q)}>{t('b5.ask')}</Button></Space.Compact>
    <div style={{ margin: '8px 0', color: '#888' }}>{t('b5.askNote')}</div>
    <Result r={r} />
    <Card size="small" style={{ marginTop: 12 }} title={t('b5.reports')} extra={<Button onClick={() => setOpen(true)}>{t('b5.newReport')}</Button>}>
      <List dataSource={reps} renderItem={(x: any) => <List.Item actions={[<a key="r" onClick={() => api.post(`/reports/${x.id}:run`).then(y => setR(y.data))}>{t('b5.run')}</a>,
        <a key="d" onClick={() => api.delete(`/reports/${x.id}`).then(load)}>{t('b5.delete')}</a>]}>{x.name} <Tag>{t(`b5.m.${x.metric}`)}</Tag><Tag>{t(`b5.d.${x.dim}`)}</Tag>{x.days}{t('b5.days')}</List.Item>} />
    </Card>
    <Modal open={open} onCancel={() => setOpen(false)} footer={null} title={t('b5.newReport')} destroyOnHidden>
      <Form layout="vertical" initialValues={{ metric: 'inquiries', dim: 'country', days: 30 }} onFinish={v => api.post('/reports', v).then(() => { setOpen(false); load() })}>
        <Form.Item name="name" label={t('b5.name')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="metric" label={t('b5.metric')}><Select options={metrics.map(m => ({ value: m, label: t(`b5.m.${m}`) }))} /></Form.Item>
        <Form.Item name="dim" label={t('b5.dim')}><Select options={dims.map(m => ({ value: m, label: t(`b5.d.${m}`) }))} /></Form.Item>
        <Form.Item name="days" label={t('b5.days')}><InputNumber min={1} max={730} /></Form.Item>
        <Form.Item name="subscribe" label={t('b5.subscribe')}><Select allowClear options={[{ value: 'weekly', label: t('b5.weekly') }, { value: 'monthly', label: t('b5.monthly') }]} /></Form.Item>
        <Button type="primary" htmlType="submit">{t('b5.save')}</Button>
      </Form>
    </Modal>
  </>
}

function Learning() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [icps, setIcps] = useState<any[]>([])
  const [period, setPeriod] = useState(new Date().toISOString().slice(0, 7))
  const load = () => api.get('/learning').then(x => setList(x.data))
  useEffect(() => { load(); api.get('/icps').then(x => setIcps(x.data)) }, [])
  const apply = (lid: string, sid: string) => Modal.confirm({ title: t('b5.applyTitle'), content: <Select id="icpSel" style={{ width: '100%' }} options={icps.map(i => ({ value: i.id, label: i.name }))} onChange={v => ((window as any).__icp = v)} />,
    onOk: () => api.post(`/learning/${lid}:apply`, { suggestion_id: sid, icp_id: (window as any).__icp }).then(() => { message.success(t('b5.applied')); load() }) })
  return <>
    <Space style={{ marginBottom: 12 }}><Input value={period} onChange={e => setPeriod(e.target.value)} style={{ width: 120 }} />
      <Button onClick={() => api.post('/reviews/monthly', { period }).then(load)}>{t('b5.genMonthly')}</Button>
      <Button type="primary" onClick={() => api.post('/learning:run', { days: 90, min_n: 20 }).then(load)}>{t('b5.runLearning')}</Button></Space>
    {list.map(r => <Card key={r.id} size="small" style={{ marginBottom: 12 }} title={<>{r.kind === 'monthly' ? t('b5.monthlyReview') : t('b5.learningReport')} · {r.period}</>}>
      {r.kind === 'monthly' ? <>
        <Descriptions size="small" column={3} items={Object.entries(r.content.kpis).map(([k, v]: any) => ({ key: k, label: t(`b5.m.${k}`), children: <>{v} {r.content.mom_pct[k] != null && <Tag color={r.content.mom_pct[k] >= 0 ? 'green' : 'red'}>{r.content.mom_pct[k]}%</Tag>}</> }))} />
        <List size="small" header={t('b5.recs')} dataSource={r.content.recommendations} renderItem={(x: any) => <List.Item>{x}</List.Item>} />
      </> : <>
        <div>{t('b5.learnLine', { sent: r.content.sent, rate: r.content.reply_rate })}</div>
        {['country', 'industry', 'seniority', 'variant'].map(d => <Table key={d} size="small" rowKey="key" pagination={false} title={() => t(`b5.ld.${d}`)} dataSource={r.content.dims[d]}
          columns={[{ title: t('b5.group'), dataIndex: 'key' }, { title: t('b5.sentN'), dataIndex: 'sent' }, { title: t('b5.replyRate'), dataIndex: 'reply_rate', render: (v: any) => v == null ? '—' : `${v}%` },
            { title: t('b5.acceptRate'), dataIndex: 'accept_rate', render: (v: any) => v == null ? '—' : `${v}%` }, { title: t('b5.sig'), dataIndex: 'significant', render: (v: boolean) => v ? <Tag color="green">{t('b5.yes')}</Tag> : <Tag>{t('b5.smallSample')}</Tag> }]} />)}
        <List size="small" header={t('b5.suggestions')} dataSource={r.content.suggestions} locale={{ emptyText: t('b5.noSugg') }}
          renderItem={(s: any) => <List.Item actions={s.type.startsWith('icp') ? [r.applied?.some((a: any) => a.id === s.id) ? <Tag key="a" color="green">{t('b5.applied')}</Tag> : <a key="a" onClick={() => apply(r.id, s.id)}>{t('b5.apply')}</a>] : []}>
            <Tag>{t(`b5.st.${s.type}`)}</Tag>{s.value} — {s.why}</List.Item>} />
        <div style={{ color: '#888' }}>{r.content.note}</div>
      </>}
    </Card>)}
  </>
}

function Schedules() {
  const { t } = useTranslation()
  const [d, setD] = useState<any>({ items: [], events: {} })
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const trig = Form.useWatch('trigger', form)
  const load = () => api.get('/agent-schedules').then(x => setD(x.data))
  useEffect(() => { load() }, [])
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b5.schNote')} />
    <Button type="primary" onClick={() => setOpen(true)} style={{ marginBottom: 12 }}>{t('b5.newSch')}</Button>
    <Table size="small" rowKey="id" dataSource={d.items} columns={[{ title: t('b5.name'), dataIndex: 'name' }, { title: t('b5.text'), dataIndex: 'text' },
      { title: t('b5.trigger'), render: (_: any, s: any) => s.trigger === 'event' ? <Tag color="purple">{d.events[s.event]}</Tag> : <Tag>{t(`b5.tr.${s.trigger}`)} {s.hour}:00 UTC</Tag> },
      { title: t('b5.autonomy'), dataIndex: 'autonomy' }, { title: t('b5.runs'), dataIndex: 'runs' },
      { title: t('b5.enabled'), render: (_: any, s: any) => <Switch size="small" checked={s.enabled} onChange={v => api.put(`/agent-schedules/${s.id}`, { ...s, enabled: v }).then(load)} /> },
      { title: '', render: (_: any, s: any) => <Space>{s.trigger !== 'event' && <a onClick={() => api.post(`/agent-schedules/${s.id}:run-now`).then(() => message.success(t('b5.taskCreated')))}>{t('b5.runNow')}</a>}
        <a onClick={() => api.delete(`/agent-schedules/${s.id}`).then(load)}>{t('b5.delete')}</a></Space> }]} />
    <Modal open={open} onCancel={() => setOpen(false)} footer={null} title={t('b5.newSch')} destroyOnHidden>
      <Form form={form} layout="vertical" initialValues={{ trigger: 'daily', hour: 1, autonomy: 'L1', max_runs_per_day: 3 }} onFinish={v => api.post('/agent-schedules', v).then(() => { setOpen(false); load() })}>
        <Form.Item name="name" label={t('b5.name')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="text" label={t('b5.text')} rules={[{ required: true }]} extra={t('b5.varsHint')}><Input.TextArea rows={2} /></Form.Item>
        <Form.Item name="trigger" label={t('b5.trigger')}><Select options={['daily', 'weekly', 'monthly', 'event'].map(x => ({ value: x, label: t(`b5.tr.${x}`) }))} /></Form.Item>
        {trig === 'event' ? <Form.Item name="event" label={t('b5.event')} rules={[{ required: true }]}><Select options={Object.entries(d.events).map(([k, v]: any) => ({ value: k, label: v }))} /></Form.Item>
          : <Space><Form.Item name="hour" label={t('b5.hourUtc')}><InputNumber min={0} max={23} /></Form.Item>
            {trig === 'weekly' && <Form.Item name="weekday" label={t('b5.weekday')} rules={[{ required: true }]}><InputNumber min={0} max={6} /></Form.Item>}</Space>}
        <Space><Form.Item name="autonomy" label={t('b5.autonomy')}><Select style={{ width: 90 }} options={['L0', 'L1', 'L2'].map(x => ({ value: x, label: x }))} /></Form.Item>
          <Form.Item name="budget" label={t('b5.budget')}><InputNumber min={1} /></Form.Item>
          <Form.Item name="max_runs_per_day" label={t('b5.maxRuns')}><InputNumber min={1} max={20} /></Form.Item></Space>
        <Button type="primary" htmlType="submit">{t('b5.save')}</Button>
      </Form>
    </Modal>
  </>
}

function StyleAndKb() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const [sp, setSp] = useState<any>()
  const [kv, setKv] = useState<any>({ items: [] })
  const [diff, setDiff] = useState<any>()
  const [lang, setLang] = useState('de')
  const load = () => { api.get('/style-profile').then(x => setSp(x.data)); api.get('/kb/versions').then(x => setKv(x.data)) }
  useEffect(() => { load() }, [])
  const f = sp?.features || {}
  return <>
    <Card size="small" title={t('b5.style')} extra={<Space>{sp?.samples > 0 && <Switch checked={sp.enabled} onChange={v => api.put('/style-profile', { enabled: v }).then(load)} />}
      <Button onClick={() => api.post('/style-profile:learn').then(load)}>{t('b5.learnStyle')}</Button></Space>}>
      {sp?.active ? <Descriptions size="small" column={3} items={[{ key: 'g', label: t('b5.greeting'), children: f.greeting }, { key: 's', label: t('b5.signOff'), children: f.sign_off },
        { key: 'f', label: t('b5.formality'), children: t(`b5.fm.${f.formality}`) }, { key: 'w', label: t('b5.avgWords'), children: f.avg_words },
        { key: 'b', label: t('b5.bullets'), children: f.uses_bullets ? t('b5.yes') : t('b5.no') }, { key: 'n', label: t('b5.samples'), children: sp.samples }]} />
        : <Alert type="info" message={t('b5.styleEmpty', { n: sp?.samples || 0 })} />}
      <div style={{ color: '#888', marginTop: 6 }}>{t('b5.styleNote')}</div>
    </Card>
    <Card size="small" style={{ marginTop: 12 }} title={t('b5.kbVersions')} extra={can('kb.manage') && <Space>
      {kv.unpublished_changes && <Tag color="orange">{t('b5.unpublished')}</Tag>}
      <Button type="primary" onClick={() => api.post('/kb/versions', {}).then(load)}>{t('b5.publish')}</Button>
      <Select value={lang} onChange={setLang} style={{ width: 80 }} options={['en', 'de', 'es', 'fr', 'pt', 'ru', 'ar', 'ja', 'it', 'pl', 'tr', 'nl'].map(x => ({ value: x, label: x }))} />
      <Button onClick={() => api.post('/kb/translations', { lang }).then(x => message.success(t('b5.translated', { n: x.data.translated })))}>{t('b5.translate')}</Button></Space>}>
      <Table size="small" rowKey="version" dataSource={kv.items} pagination={false} columns={[{ title: t('b5.version'), dataIndex: 'version', render: (v: number) => `v${v}` },
        { title: t('b5.items'), dataIndex: 'item_count' }, { title: t('b5.note'), dataIndex: 'note' }, { title: t('b5.time'), dataIndex: 'created_at', render: (v: string) => v.slice(0, 19) },
        { title: '', render: (_: any, v: any) => <Space>{v.version > 1 && <a onClick={() => api.get(`/kb/versions/${v.version - 1}`, { params: { to: v.version } }).then(x => setDiff(x.data.diff))}>{t('b5.diffPrev')}</a>}
          {can('kb.manage') && <a onClick={() => Modal.confirm({ title: t('b5.rollbackQ', { v: v.version }), onOk: () => api.post(`/kb/versions/${v.version}:rollback`).then(load) })}>{t('b5.rollback')}</a>}</Space> }]} />
      {diff && <Alert style={{ marginTop: 8 }} closable onClose={() => setDiff(undefined)} message={`${t('b5.added')}: ${diff.added.join('、') || '—'}；${t('b5.changed')}: ${diff.changed.join('、') || '—'}；${t('b5.removed')}: ${diff.removed.join('、') || '—'}`} />}
      <div style={{ color: '#888', marginTop: 6 }}>{t('b5.kbNote')}</div>
    </Card>
  </>
}

function Tools() {
  const { t } = useTranslation()
  const [reps, setReps] = useState<any[]>([])
  const [rid, setRid] = useState<string>()
  const [q, setQ] = useState('')
  const [qa, setQa] = useState<any[]>([])
  const [chain, setChain] = useState<any>()
  const [accs, setAccs] = useState<any[]>([])
  const [acc, setAcc] = useState<string>()
  const [att, setAtt] = useState<any>()
  const [opps, setOpps] = useState<any[]>([])
  const [wp, setWp] = useState<any>()
  useEffect(() => {
    api.get('/research').then(x => setReps(x.data)).catch(() => null)
    api.get('/accounts', { params: { size: 100 } }).then(x => setAccs(x.data.items || [])).catch(() => null)
    api.get('/opportunities', { params: { size: 100 } }).then(x => setOpps(x.data.items || [])).catch(() => null)
  }, [])
  const pick = (id: string) => { setRid(id); api.get(`/research/${id}/questions`).then(x => setQa(x.data)); const r = reps.find(x => x.id === id); if (r?.company_id) api.get(`/companies/${r.company_id}/decision-chain`).then(x => setChain(x.data)) }
  return <>
    <Card size="small" title={t('b5.researchQa')}>
      <Select style={{ width: 360 }} placeholder={t('b5.pickReport')} value={rid} onChange={pick} options={reps.map(r => ({ value: r.id, label: `${r.company_name || r.company_id} v${r.version}` }))} />
      {rid && <Space.Compact style={{ width: '100%', marginTop: 8 }}><Input value={q} onChange={e => setQ(e.target.value)} placeholder={t('b5.qaPh')} />
        <Button type="primary" onClick={() => api.post(`/research/${rid}/ask`, { q }).then(x => { setQa([...qa, x.data]); setQ('') })}>{t('b5.ask')}</Button></Space.Compact>}
      <List size="small" dataSource={qa} renderItem={(x: any) => <List.Item><div><b>{x.question}</b><div>{x.answer}</div>{!x.grounded && <Tag color="orange">{t('b5.ungrounded')}</Tag>}</div></List.Item>} />
      {chain && <Table size="small" rowKey="id" pagination={false} title={() => t('b5.chain')} dataSource={chain.nodes} footer={() => (chain.suggestions || []).join('；') + ` （${t('b5.inferred')}）`}
        columns={[{ title: t('b5.name'), dataIndex: 'name' }, { title: t('b5.title'), dataIndex: 'title' }, { title: t('b5.role'), dataIndex: 'role', render: (v: string) => <Tag>{t(`b5.role_${v}`)}</Tag> }]} />}
    </Card>
    <Card size="small" title={t('b5.attQuote')} style={{ marginTop: 12 }}>
      <Space><Select showSearch optionFilterProp="label" style={{ width: 280 }} placeholder={t('b5.pickAccount')} value={acc} onChange={setAcc} options={accs.map(a => ({ value: a.id, label: a.name }))} />
        <Upload showUploadList={false} customRequest={({ file }: any) => { const fd = new FormData(); fd.append('file', file); if (acc) { fd.append('account_id', acc); fd.append('create', 'true') }
          api.post('/quotes:from-attachment', fd).then(x => setAtt(x.data)) }}><Button>{t('b5.upload')}</Button></Upload></Space>
      {att && <>{att.injection?.length > 0 && <Alert type="warning" style={{ marginTop: 8 }} message={t('b5.injWarn')} />}
        <Table size="small" rowKey="source_line" pagination={false} style={{ marginTop: 8 }} dataSource={att.items} columns={[{ title: 'SKU', dataIndex: 'sku' }, { title: t('b5.product'), dataIndex: 'name' },
          { title: t('b5.qty'), dataIndex: 'qty' }, { title: t('b5.unitPrice'), dataIndex: 'unit_price' }, { title: t('b5.target'), dataIndex: 'customer_target_price' }]} />
        {att.unmatched?.length > 0 && <Alert style={{ marginTop: 8 }} message={t('b5.unmatched')} description={att.unmatched.join(' | ')} />}
        <div style={{ marginTop: 6 }}>{att.quote_no ? <Tag color="green">{t('b5.quoteDraft', { no: att.quote_no })}</Tag> : null} <span style={{ color: '#888' }}>{att.note}</span></div></>}
    </Card>
    <Card size="small" title={t('b5.winProb')} style={{ marginTop: 12 }}>
      <Select style={{ width: 360 }} placeholder={t('b5.pickOpp')} onChange={(id: string) => api.get(`/opportunities/${id}/win-probability`).then(x => setWp(x.data))} options={opps.map(o => ({ value: o.id, label: `${o.name} · ${o.stage}` }))} />
      {wp && <div style={{ marginTop: 8 }}><Tag color="blue" style={{ fontSize: 16 }}>{Math.round(wp.probability * 100)}%</Tag>{wp.calibrated ? <Tag color="green">{t('b5.calibrated')}</Tag> : <Tag>{t('b5.baseOnly')}</Tag>}
        <List size="small" dataSource={wp.factors} renderItem={(x: any) => <List.Item>{x}</List.Item>} /></div>}
    </Card>
  </>
}

function Evals() {
  const { t } = useTranslation()
  const [d, setD] = useState<any>()
  useEffect(() => { api.get('/ai/evals').then(x => setD(x.data)) }, [])
  if (!d) return null
  return <>
    <Table size="small" rowKey="suite" pagination={false} dataSource={d.suites} locale={{ emptyText: t('b5.noEvals') }} columns={[{ title: t('b5.suite'), dataIndex: 'suite', render: (v: string) => t(`b5.su.${v}`) },
      { title: t('b5.promptVer'), dataIndex: 'prompt_version' }, { title: t('b5.model'), dataIndex: 'model' }, { title: t('b5.score'), render: (_: any, r: any) => `${Math.round(r.score * 100)}% / ${Math.round(r.threshold * 100)}%` },
      { title: t('b5.result'), dataIndex: 'ok', render: (v: boolean) => v ? <Tag color="green">{t('b5.passed')}</Tag> : <Tag color="red">{t('b5.fail')}</Tag> }, { title: t('b5.time'), dataIndex: 'at', render: (v: string) => v.slice(0, 19) }]} />
    <Descriptions size="small" style={{ marginTop: 12 }} title={t('b5.promptVers')} column={3} items={Object.entries(d.prompt_versions).map(([k, v]: any) => ({ key: k, label: k, children: v }))} />
    <div style={{ color: '#888', marginTop: 6 }}>{t('b5.evalNote')}</div>
  </>
}

export default function AiLabPage() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const tabs = [
    can('analytics.read') && { key: 'ask', label: t('b5.tabAsk'), children: <AskData /> },
    can('analytics.team') && { key: 'learn', label: t('b5.tabLearn'), children: <Learning /> },
    can('agent.run') && { key: 'sch', label: t('b5.tabSch'), children: <Schedules /> },
    { key: 'style', label: t('b5.tabStyle'), children: <StyleAndKb /> },
    { key: 'tools', label: t('b5.tabTools'), children: <Tools /> },
    { key: 'eval', label: t('b5.tabEval'), children: <Evals /> },
  ].filter(Boolean) as any[]
  return <Card title={t('menu.aiLab')}><Tabs items={tabs} /></Card>
}
