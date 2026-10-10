/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Switch, Table, Tabs, Tag, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const lines = (v?: string) => (v || '').split('\n').map(s => s.trim()).filter(Boolean)

function ProductList({ manage }: { manage: boolean }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [q, setQ] = useState('')
  const [edit, setEdit] = useState<any>()
  const [detail, setDetail] = useState<any>()
  const [form] = Form.useForm()
  const load = () => { api.get('/products', { params: { q: q || undefined } }).then(r => setRows(r.data)) }
  useEffect(load, [q])
  const save = async () => {
    const v = await form.validateFields()
    const body = { ...v, certifications: (v.certifications || []), params: {} }
    if (edit?.id) await api.put(`/products/${edit.id}`, { ...edit, ...body }); else await api.post('/products', body)
    message.success(t('b2.saved')); setEdit(undefined); load()
  }
  const open = (r: any) => api.get(`/products/${r.id}`).then(x => setDetail(x.data))
  return <>
    <Input.Search placeholder={t('b2.searchProduct')} style={{ width: 280, marginBottom: 8 }} onSearch={setQ} allowClear />
    {manage && <Button type="primary" style={{ marginLeft: 8 }} onClick={() => { form.resetFields(); setEdit({}) }}>{t('b2.newProduct')}</Button>}
    <Table rowKey="id" size="small" dataSource={rows} onRow={(r: any) => ({ onClick: () => open(r) })} columns={[
      { title: t('b2.nameZh'), dataIndex: 'name_zh' }, { title: t('b2.nameEn'), dataIndex: 'name_en' },
      { title: t('b2.model'), dataIndex: 'model' }, { title: 'HS', dataIndex: 'hs_code', render: (v, r: any) => v || (r.hs_candidates?.[0] && <Tag color="orange">{t('b2.hsSuggest')} {r.hs_candidates[0].code}</Tag>) },
      { title: 'MOQ', dataIndex: 'moq' }, { title: t('b2.certs'), dataIndex: 'certifications', render: v => (v || []).map((c: string) => <Tag key={c}>{c}</Tag>) },
    ]} />
    <Modal open={!!edit} title={t('b2.product')} onOk={save} onCancel={() => setEdit(undefined)} width={640}>
      <Form form={form} layout="vertical">
        <Form.Item name="name_zh" label={t('b2.nameZh')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="name_en" label={t('b2.nameEn')}><Input /></Form.Item>
        <Form.Item name="model" label={t('b2.model')}><Input /></Form.Item>
        <Form.Item name="category" label={t('b2.category')}><Input /></Form.Item>
        <Form.Item name="certifications" label={t('b2.certs')}><Select mode="tags" /></Form.Item>
        <Form.Item name="moq" label="MOQ"><InputNumber min={0} /></Form.Item>
        <Form.Item name="lead_time_days" label={t('b2.leadTime')}><Input placeholder="25-30" /></Form.Item>
        <Form.Item name="description" label={t('b2.desc')}><Input.TextArea rows={3} /></Form.Item>
      </Form>
    </Modal>
    <Modal open={!!detail} title={detail?.name_zh} footer={null} onCancel={() => setDetail(undefined)} width={760}>
      {detail && <>
        <Alert type="info" showIcon message={t('b2.hsNote')} description={(detail.hs_candidates || []).map((h: any) => `${h.code}：${h.reason}`).join('；') || '—'} />
        <h4 style={{ marginTop: 12 }}>{t('b2.sellingPoints')}</h4>
        {(detail.selling_points || []).map((p: any, i: number) => <div key={i}><Tag color={p.kind === 'fact' ? 'blue' : 'default'}>{t('b2.k_' + p.kind)}</Tag>{p.text}{p.source && <span style={{ color: '#999' }}>（{p.source}）</span>}</div>)}
        <h4 style={{ marginTop: 12 }}>SKU</h4>
        <Table rowKey="id" size="small" pagination={false} dataSource={detail.skus} columns={[
          { title: 'SKU', dataIndex: 'sku' }, { title: t('b2.factoryPrice'), dataIndex: 'factory_price' },
          { title: t('b2.tiers'), dataIndex: 'tier_prices', render: v => (v || []).map((x: any) => `≥${x.min_qty}: ${x.price}`).join('，') },
          { title: t('b2.floorPrice'), dataIndex: 'floor_price', render: v => v ?? t('b2.hidden') },
        ]} />
      </>}
    </Modal>
  </>
}

function Objections({ manage }: { manage: boolean }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const load = () => { api.get('/objections').then(r => setRows(r.data)) }
  useEffect(load, [])
  const draft = async (r: any) => { await api.post(`/objections/${r.id}:draft`); load() }
  const confirm = async (r: any) => { await api.put(`/objections/${r.id}`, { ...r, status: 'confirmed' }); load() }
  return <Table rowKey="id" size="small" dataSource={rows} columns={[
    { title: t('b2.category'), dataIndex: 'category', render: v => <Tag>{t('b2.oc_' + v)}</Tag>, width: 100 },
    { title: t('b2.question'), dataIndex: 'question', width: 220 }, { title: t('b2.answer'), dataIndex: 'answer' },
    { title: t('b2.status'), dataIndex: 'status', render: v => <Tag color={v === 'confirmed' ? 'green' : 'default'}>{t('b2.s_' + v)}</Tag>, width: 90 },
    manage ? { title: '', width: 170, render: (_: any, r: any) => <><Button size="small" onClick={() => draft(r)}>{t('b2.aiDraft')}</Button>
      {r.answer && r.status !== 'confirmed' && <Button size="small" type="link" onClick={() => confirm(r)}>{t('b2.confirm')}</Button>}</> } : {},
  ]} />
}

function Boundary({ manage }: { manage: boolean }) {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  useEffect(() => { api.get('/kb/boundary').then(r => form.setFieldsValue({ ...r.data, ...Object.fromEntries(['can_say', 'cannot_say', 'forbidden_words', 'human_topics'].map(k => [k, (r.data[k] || []).join('\n')])) })) }, [])
  const save = async () => {
    const v = form.getFieldsValue()
    await api.put('/kb/boundary', { ...v, can_say: lines(v.can_say), cannot_say: lines(v.cannot_say), forbidden_words: lines(v.forbidden_words), human_topics: lines(v.human_topics) })
    message.success(t('b2.saved'))
  }
  return <Form form={form} layout="vertical" disabled={!manage} style={{ maxWidth: 640 }}>
    <Alert type="info" showIcon message={t('b2.boundaryHint')} style={{ marginBottom: 12 }} />
    <Form.Item name="can_say" label={t('b2.canSay')}><Input.TextArea rows={3} /></Form.Item>
    <Form.Item name="cannot_say" label={t('b2.cannotSay')}><Input.TextArea rows={3} /></Form.Item>
    <Form.Item name="forbidden_words" label={t('b2.forbidden')}><Input.TextArea rows={3} /></Form.Item>
    <Form.Item name="human_topics" label={t('b2.humanTopics')}><Input.TextArea rows={2} /></Form.Item>
    <Form.Item name="public_price_range" label={t('b2.publicPrice')} valuePropName="checked"><Switch /></Form.Item>
    <Form.Item name="lead_time_range" label={t('b2.leadTimeRange')}><Input /></Form.Item>
    <Button type="primary" onClick={save}>{t('b2.save')}</Button>
  </Form>
}

function Imports() {
  const { t } = useTranslation()
  const [docs, setDocs] = useState<any[]>([])
  const [batches, setBatches] = useState<any[]>([])
  const load = () => { api.get('/kb/documents').then(r => setDocs(r.data)); api.get('/import-batches').then(r => setBatches(r.data)) }
  useEffect(load, [])
  const up = (url: string, field: string) => async ({ file, onSuccess }: any) => {
    const fd = new FormData(); fd.append(field, file); await api.post(url, fd); onSuccess?.({}); message.success(t('b2.uploaded')); load()
  }
  const undo = async (b: any) => { await api.post(`/import-batches/${b.id}:undo`); load() }
  return <>
    <Alert type="info" showIcon message={t('b2.docHint')} style={{ marginBottom: 8 }} />
    <Upload multiple customRequest={up('/kb/documents', 'files')} showUploadList={false}><Button>{t('b2.uploadDocs')}</Button></Upload>
    <Upload customRequest={up('/kb/deals:import', 'file')} showUploadList={false} accept=".csv,.xlsx"><Button style={{ marginLeft: 8 }}>{t('b2.importDeals')}</Button></Upload>
    <Table rowKey="id" size="small" style={{ marginTop: 12 }} dataSource={docs} columns={[
      { title: t('b2.file'), dataIndex: 'filename' }, { title: t('b2.status'), dataIndex: 'status', render: v => <Tag>{t('b2.d_' + v)}</Tag> },
      { title: t('b2.itemsCreated'), dataIndex: 'items_created' }, { title: t('b2.error'), dataIndex: 'error' },
    ]} />
    <h4>{t('b2.batches')}</h4>
    <Table rowKey="id" size="small" dataSource={batches} columns={[
      { title: t('b2.type'), dataIndex: 'kind' }, { title: t('b2.stats'), dataIndex: 'stats', render: v => JSON.stringify({ ...v, errors: (v.errors || []).length, suspects: (v.suspects || []).length }) },
      { title: t('b2.status'), dataIndex: 'status' }, { title: t('b2.time'), dataIndex: 'created_at', render: v => new Date(v).toLocaleString() },
      { title: '', render: (_: any, b: any) => b.status === 'done' && <Button size="small" danger onClick={() => undo(b)}>{t('b2.undo')}</Button> },
    ]} />
  </>
}

export default function ProductsPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const manage = can('product.manage')
  return <Tabs items={[
    { key: 'p', label: t('b2.products'), children: <ProductList manage={manage} /> },
    { key: 'o', label: t('b2.objections'), children: <Objections manage={manage} /> },
    { key: 'b', label: t('b2.boundary'), children: <Boundary manage={manage} /> },
    { key: 'i', label: t('b2.docImport'), children: <Imports /> },
  ]} />
}
