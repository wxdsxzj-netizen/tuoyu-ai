/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const money = (m: number, c = '') => `${(m / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })} ${c}`

function Samples() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [ship, setShip] = useState<any>()
  const [form] = Form.useForm()
  const load = () => { api.get('/samples').then(r => setRows(r.data)) }
  useEffect(load, [])
  const move = async (r: any, to: string, extra: any = {}) => { await api.post(`/samples/${r.id}:move`, { to, ...extra }); load() }
  const next: any = { requested: 'shipped', shipped: 'received', received: 'feedback', feedback: 'converted' }
  return <>
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('b2.items'), dataIndex: 'items', render: v => (v || []).map((i: any) => `${i.name}×${i.qty}`).join('，') },
      { title: t('b2.fee'), dataIndex: 'fee_type', render: v => t('b2.fee_' + v) },
      { title: t('b2.tracking'), render: (_: any, r: any) => r.tracking_no ? `${r.courier || ''} ${r.tracking_no}` : '—' },
      { title: t('b2.status'), dataIndex: 'status', render: v => <Tag>{t('b2.sm_' + v)}</Tag> },
      { title: '', render: (_: any, r: any) => next[r.status] && <Button size="small" onClick={() => next[r.status] === 'shipped' ? setShip(r) : move(r, next[r.status])}>{t('b2.sm_' + next[r.status])}</Button> },
    ]} />
    <Modal open={!!ship} title={t('b2.sm_shipped')} onCancel={() => setShip(undefined)} onOk={async () => { await move(ship, 'shipped', form.getFieldsValue()); setShip(undefined) }}>
      <Form form={form} layout="vertical">
        <Form.Item name="courier" label={t('b2.courier')}><Select options={['DHL', 'FedEx', 'UPS', 'TNT', 'EMS', 'SF'].map(v => ({ value: v, label: v }))} /></Form.Item>
        <Form.Item name="tracking_no" label={t('b2.trackingNo')} rules={[{ required: true }]}><Input /></Form.Item>
      </Form>
    </Modal>
  </>
}

function Orders() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [pay, setPay] = useState<any>()
  const [amt, setAmt] = useState<number>()
  const load = () => { api.get('/orders').then(r => setRows(r.data)) }
  useEffect(load, [])
  const status = async (r: any, s: string) => { await api.post(`/orders/${r.id}:status`, { status: s }); load() }
  return <>
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('b2.orderNo'), dataIndex: 'order_no' }, { title: t('b2.total'), render: (_: any, r: any) => money(r.total_minor, r.currency) },
      { title: t('b2.paid'), render: (_: any, r: any) => money(r.paid_minor, r.currency) },
      { title: t('b2.status'), dataIndex: 'status', render: v => <Tag>{t('b2.os_' + v)}</Tag> },
      { title: t('b2.overdue'), dataIndex: 'overdue', render: v => v?.length ? <Tag color="red">{money(v[v.length - 1].short_minor)}</Tag> : '—' },
      { title: '', render: (_: any, r: any) => <>
        <Button size="small" onClick={() => setPay(r)}>{t('b2.addPayment')}</Button>
        {r.status === 'confirmed' && <Button size="small" type="link" onClick={() => status(r, 'producing')}>{t('b2.os_producing')}</Button>}
        {r.status === 'producing' && <Button size="small" type="link" onClick={() => status(r, 'shipped')}>{t('b2.os_shipped')}</Button>}
        {r.status === 'shipped' && r.paid_minor >= r.total_minor && <Button size="small" type="link" onClick={() => status(r, 'completed')}>{t('b2.os_completed')}</Button>}
      </> },
    ]} />
    <Modal open={!!pay} title={t('b2.addPayment')} onCancel={() => setPay(undefined)} onOk={async () => {
      await api.post(`/orders/${pay.id}/payments`, { amount_minor: Math.round((amt || 0) * 100) }); setPay(undefined); load()
    }}><InputNumber min={0} style={{ width: 200 }} value={amt} onChange={v => setAmt(v ?? undefined)} addonAfter={pay?.currency} /></Modal>
  </>
}

function Settings({ admin }: { admin: boolean }) {
  const { t } = useTranslation()
  const [cfg, setCfg] = useState<any>()
  const [fields, setFields] = useState<any[]>([])
  const [obj, setObj] = useState('account')
  const [fx, setFx] = useState<any[]>([])
  const [form] = Form.useForm()
  const load = () => {
    api.get('/crm/settings').then(r => setCfg(r.data)); api.get('/crm/fields', { params: { object_type: obj } }).then(r => setFields(r.data))
    api.get('/fx-rates').then(r => setFx(r.data))
  }
  useEffect(load, [obj])
  const saveStage = async (k: string, patch: any) => { await api.put('/crm/settings', { stages: { [k]: { ...cfg.stages[k], ...patch } } }); load() }
  const addField = async () => { await api.post('/crm/fields', { ...(await form.validateFields()), object_type: obj }); form.resetFields(); load() }
  if (!cfg) return null
  return <>
    <h4>{t('b2.stages')}</h4>
    <Table rowKey="k" size="small" pagination={false} dataSource={Object.entries(cfg.stages).map(([k, v]: any) => ({ k, ...v }))} columns={[
      { title: t('b2.stage'), dataIndex: 'label' },
      { title: t('b2.enabled'), render: (_: any, r: any) => <Switch size="small" disabled={!admin || ['won', 'lost', 'new_inquiry'].includes(r.k)} checked={r.enabled} onChange={v => saveStage(r.k, { enabled: v })} /> },
      { title: t('b2.requiredFields'), render: (_: any, r: any) => <Select mode="multiple" size="small" disabled={!admin} style={{ minWidth: 240 }} value={r.required}
        onChange={v => saveStage(r.k, { required: v })} options={['amount', 'expected_close_date', 'contact_id', 'products'].map(x => ({ value: x, label: t('b2.f_' + x) }))} /> },
    ]} />
    <h4 style={{ marginTop: 16 }}>{t('b2.gradeRules')}</h4>
    <GradeEditor rules={cfg.grade_rules} admin={admin} onSaved={load} />
    <h4 style={{ marginTop: 16 }}>{t('b2.customFields')}</h4>
    <Select value={obj} onChange={setObj} style={{ width: 160, marginBottom: 8 }} options={['account', 'opportunity', 'contact'].map(v => ({ value: v, label: t('b2.o_' + v) }))} />
    <Table rowKey="id" size="small" pagination={false} dataSource={fields} columns={[
      { title: 'Key', dataIndex: 'key' }, { title: t('b2.label'), dataIndex: 'label' }, { title: t('b2.type'), dataIndex: 'field_type' },
      { title: t('b2.required'), dataIndex: 'required', render: v => v ? '✓' : '' }, { title: t('b2.visibleRoles'), dataIndex: 'visible_roles', render: v => v?.join(',') || t('b2.all') },
      admin ? { title: '', render: (_: any, r: any) => <Button size="small" danger onClick={async () => { await api.delete(`/crm/fields/${r.id}`); load() }}>{t('b2.delete')}</Button> } : {},
    ]} />
    {admin && <Form form={form} layout="inline" style={{ marginTop: 8 }}>
      <Form.Item name="key" rules={[{ required: true, pattern: /^[a-z][a-z0-9_]*$/ }]}><Input placeholder="key" /></Form.Item>
      <Form.Item name="label" rules={[{ required: true }]}><Input placeholder={t('b2.label')} /></Form.Item>
      <Form.Item name="field_type" initialValue="text"><Select style={{ width: 100 }} options={['text', 'number', 'date', 'select', 'bool'].map(v => ({ value: v, label: v }))} /></Form.Item>
      <Form.Item name="required" valuePropName="checked" initialValue={false}><Switch checkedChildren={t('b2.required')} /></Form.Item>
      <Button onClick={addField}>{t('b2.addField')}</Button>
    </Form>}
    <h4 style={{ marginTop: 16 }}>{t('b2.fx')}</h4>
    <Table rowKey="currency" size="small" pagination={false} dataSource={fx} columns={[
      { title: t('b2.currency'), dataIndex: 'currency' }, { title: t('b2.cnyPer'), dataIndex: 'cny_per_unit' },
      { title: t('b2.date'), dataIndex: 'date' }, { title: t('b2.source'), dataIndex: 'source', render: v => v === 'mock' ? <Tag>{t('b2.mockFx')}</Tag> : v },
    ]} />
    {admin && <Button style={{ marginTop: 8 }} onClick={async () => { await api.post('/fx-rates:refresh'); load() }}>{t('b2.refreshFx')}</Button>}
  </>
}

function AccountPicker({ value, onChange, exclude }: { value?: string, onChange: (v: string) => void, exclude?: string }) {
  const { t } = useTranslation()
  const [opts, setOpts] = useState<any[]>([])
  const search = async (q: string) => {
    const r = await api.get('/accounts', { params: { q: q || undefined, size: 20 } })
    setOpts(r.data.items.filter((a: any) => a.id !== exclude).map((a: any) => ({ value: a.id, label: `${a.name}${a.domain ? ' · ' + a.domain : ''}${a.country ? ' · ' + a.country : ''}${a.grade ? ' · ' + a.grade : ''}` })))
  }
  useEffect(() => { search('') }, [exclude])
  return <Select showSearch filterOption={false} style={{ width: 320 }} value={value} onChange={onChange} onSearch={search} options={opts} placeholder={t('b3.searchAccount')} />
}

const GRADE_KEYS: Record<string, string[]> = { A: ['min_amount', 'min_deals', 'active_days'], B: ['min_amount', 'min_deals', 'active_days'], C: ['min_amount', 'min_deals', 'active_days'] }

function GradeEditor({ rules, admin, onSaved }: { rules: any, admin: boolean, onSaved: () => void }) {
  const { t } = useTranslation()
  const [r, setR] = useState<any>(rules)
  useEffect(() => setR(rules), [rules])
  const set = (g: string, k: string, v: any) => {
    const cur = { ...(r[g] || {}) }
    if (v === null || v === undefined || v === '') delete cur[k]; else cur[k] = v
    setR({ ...r, [g]: cur })
  }
  const save = async () => { await api.put('/crm/settings', { grade_rules: { ...r, D: {} } }); message.success(t('b1.saved')); onSaved() }
  return <>
    <Table rowKey="g" size="small" pagination={false} dataSource={['A', 'B', 'C'].map(g => ({ g }))} columns={[
      { title: t('b3.grade'), dataIndex: 'g', render: g => <Tag color={{ A: 'gold', B: 'blue', C: 'default' }[g as string]}>{g}</Tag> },
      ...GRADE_KEYS.A.map(k => ({ title: t('b3.g_' + k), render: (_: any, row: any) => <InputNumber size="small" min={0} disabled={!admin} value={r?.[row.g]?.[k]} onChange={v => set(row.g, k, v)} /> })),
    ]} />
    <div style={{ color: '#888', margin: '6px 0' }}>{t('b3.gradeExplain')}</div>
    {admin && <Space><Button type="primary" onClick={save}>{t('b1.save')}</Button>
      <Button onClick={async () => { const x = await api.post('/crm/accounts:regrade'); message.success(t('b3.regraded', { n: x.data.changed ?? x.data.updated ?? JSON.stringify(x.data) })) }}>{t('b2.regrade')}</Button></Space>}
  </>
}

function AccountImport() {
  const { t } = useTranslation()
  const [res, setRes] = useState<any>()
  const [merge, setMerge] = useState<any>({})
  return <>
    <Alert type="info" showIcon message={t('b2.accImportHint')} style={{ marginBottom: 8 }} />
    <Upload accept=".csv,.xlsx" showUploadList={false} customRequest={async ({ file, onSuccess }: any) => {
      const fd = new FormData(); fd.append('file', file); const r = await api.post('/accounts:import', fd); setRes(r.data); onSuccess?.({})
    }}><Button>{t('b2.importAccounts')}</Button></Upload>
    {res && <pre>{JSON.stringify(res.stats, null, 1)}</pre>}
    <h4 style={{ marginTop: 16 }}>{t('b2.merge')}</h4>
    <Form layout="inline">
      <Form.Item label={t('b2.master')}><AccountPicker value={merge.master_id} onChange={v => setMerge({ ...merge, master_id: v })} exclude={merge.merged_id} /></Form.Item>
      <Form.Item label={t('b2.merged')}><AccountPicker value={merge.merged_id} onChange={v => setMerge({ ...merge, merged_id: v })} exclude={merge.master_id} /></Form.Item>
      <Button disabled={!merge.master_id || !merge.merged_id} onClick={() => Modal.confirm({ title: t('b2.doMerge'), content: t('b3.mergeConfirm'),
        onOk: async () => { const r = await api.post('/accounts:merge', merge); message.success(t('b2.m_' + r.data.status)); setMerge({}) } })}>{t('b2.doMerge')}</Button>
    </Form>
  </>
}

export default function CrmPlusPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  return <Tabs items={[
    { key: 's', label: t('b2.samples'), children: <Samples /> },
    { key: 'o', label: t('b2.orders'), children: <Orders /> },
    { key: 'i', label: t('b2.accImport'), children: <AccountImport /> },
    { key: 'c', label: t('b2.crmSettings'), children: <Settings admin={can('crm.admin')} /> },
  ]} />
}
