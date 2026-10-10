/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Col, DatePicker, Drawer, Form, Input, InputNumber, Modal, Radio, Row, Select, Space, Table, Tabs, Tag, Typography, message } from 'antd'
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const STAGES = ['new_inquiry', 'needs_confirmed', 'quotation', 'sampling', 'negotiation', 'won', 'lost']
const LOST = ['price', 'lead_time', 'quality', 'certification', 'payment_terms', 'cancelled', 'competitor', 'no_response', 'other']
const INCOTERMS = ['EXW', 'FCA', 'FOB', 'CFR', 'CIF', 'CPT', 'CIP', 'DAP', 'DPU', 'DDP']
const QS_COLOR: Record<string, string> = { draft: 'default', pending_approval: 'orange', rejected: 'red', approved: 'blue', sent: 'geekblue', locked: 'green', converted: 'purple', superseded: 'default', void: 'default' }

async function download(url: string, name: string) {
  const r = await api.get(url, { responseType: 'blob' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(r.data); a.download = name; a.click(); URL.revokeObjectURL(a.href)
}

function Accounts() {
  const { t } = useTranslation()
  const { can, me } = useAuth()
  const [pool, setPool] = useState('private')
  const [rows, setRows] = useState<any[]>([])
  const [view, setView] = useState<any>()
  const [creating, setCreating] = useState(false)
  const load = useCallback(async () => setRows((await api.get('/accounts', { params: { pool, size: 100 } })).data.items), [pool])
  useEffect(() => { load() }, [load])
  const open = async (id: string) => setView((await api.get(`/accounts/${id}`)).data)
  const act = async (id: string, a: string) => { await api.post(`/accounts/${id}:${a}`); message.success(t('p4.done')); load(); setView(undefined) }
  return <>
    <Space style={{ marginBottom: 12 }}>
      <Radio.Group value={pool} onChange={(e) => setPool(e.target.value)} optionType="button" options={[{ value: 'private', label: t('p4.private') }, { value: 'public', label: t('p4.public') }]} />
      {can('crm.manage') && <Button type="primary" onClick={() => setCreating(true)}>{t('p4.newAccount')}</Button>}
    </Space>
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('p4.accountName'), dataIndex: 'name', render: (v: string, r: any) => <a onClick={() => open(r.id)}>{v}</a> },
      { title: t('p4.domain'), dataIndex: 'domain' }, { title: t('p4.country'), dataIndex: 'country' },
      { title: t('p4.grade'), dataIndex: 'grade', render: (v: string) => <Tag color={v === 'A' ? 'red' : v === 'B' ? 'orange' : 'default'}>{t('p4.gradeN', { g: v })}</Tag> },
      { title: t('p4.stage'), dataIndex: 'stage', render: (v: string) => t(`p4.as_${v}`) },
      { title: t('p4.source'), dataIndex: 'source' },
      { title: t('p4.lastFollow'), dataIndex: 'last_valid_followup_at', render: (v: string) => v && new Date(v).toLocaleDateString() },
      { title: '', render: (_: any, r: any) => can('crm.manage') && (r.pool_status === 'public'
        ? <Button size="small" onClick={() => act(r.id, 'claim')}>{t('p4.claim')}</Button>
        : r.owner_id === me?.user.id && <Button size="small" onClick={() => act(r.id, 'release')}>{t('p4.release')}</Button>) },
    ]} />
    <Modal open={creating} title={t('p4.newAccount')} footer={null} onCancel={() => setCreating(false)} destroyOnClose>
      <Form layout="vertical" onFinish={async (v) => { await api.post('/accounts', v); setCreating(false); load() }}>
        <Form.Item name="name" label={t('p4.accountName')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="domain" label={t('p4.domain')}><Input placeholder="example.com" /></Form.Item>
        <Form.Item name="country" label={t('p4.country')}><Input maxLength={2} placeholder="DE" /></Form.Item>
        <Button type="primary" htmlType="submit">{t('p4.save')}</Button>
      </Form>
    </Modal>
    <Drawer open={!!view} width={800} title={view?.account.name} onClose={() => setView(undefined)}>
      {view && <Tabs items={[
        { key: 't', label: t('p4.timeline'), children: view.timeline.map((m: any, i: number) => <Card key={i} size="small" style={{ marginBottom: 6 }}
          title={<Space><Tag>{t(`p4.dir_${m.direction}`)}</Tag>{m.subject}</Space>} extra={new Date(m.created_at).toLocaleString()}>
          <Typography.Paragraph ellipsis={{ rows: 3 }} style={{ margin: 0 }}>{m.body}</Typography.Paragraph></Card>) },
        { key: 'c', label: t('p4.contacts'), children: <Table rowKey="id" size="small" pagination={false} dataSource={view.contacts}
          columns={[{ title: t('p4.name'), dataIndex: 'name' }, { title: t('p4.title'), dataIndex: 'title' }, { title: 'Email', dataIndex: 'email' }]} /> },
        { key: 'o', label: t('p4.opps'), children: <Table rowKey="id" size="small" pagination={false} dataSource={view.opportunities}
          columns={[{ title: t('p4.oppName'), dataIndex: 'name' }, { title: t('p4.stage'), dataIndex: 'stage', render: (v: string) => t(`p4.st_${v}`) },
            { title: t('p4.amount'), render: (_: any, r: any) => `${r.currency} ${r.amount.toLocaleString()}` },
            { title: t('p4.qualified'), dataIndex: 'is_qualified', render: (v: boolean) => v && <Tag color="green">{t('p4.qualified')}</Tag> }]} /> },
        { key: 'q', label: t('p4.quotes'), children: <Table rowKey="id" size="small" pagination={false} dataSource={view.quotes}
          columns={[{ title: t('p4.quoteNo'), render: (_: any, r: any) => `${r.quote_no} v${r.doc_version}` }, { title: t('p4.total'), render: (_: any, r: any) => `${r.currency} ${r.total.toLocaleString()}` },
            { title: t('p3.status'), dataIndex: 'status', render: (v: string) => <Tag color={QS_COLOR[v]}>{t(`p4.qs_${v}`)}</Tag> }]} /> },
      ]} />}
    </Drawer>
  </>
}

function Pipeline() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [lost, setLost] = useState<{ id: string; reason?: string } | null>(null)
  const [creating, setCreating] = useState(false)
  const [accounts, setAccounts] = useState<any[]>([])
  const load = useCallback(async () => setRows((await api.get('/opportunities', { params: { size: 200 } })).data.items), [])
  useEffect(() => { load() }, [load])
  const move = async (id: string, stage: string, lost_reason?: string) => {
    if (stage === 'lost' && !lost_reason) { setLost({ id }); return }
    await api.post(`/opportunities/${id}:stage`, { stage, lost_reason }); setLost(null); load()
  }
  return <>
    {can('crm.manage') && <Button style={{ marginBottom: 12 }} type="primary" onClick={async () => { setAccounts((await api.get('/accounts', { params: { size: 100 } })).data.items); setCreating(true) }}>{t('p4.newOpp')}</Button>}
    <div style={{ display: 'flex', gap: 8, overflowX: 'auto' }}>
      {STAGES.map((s) => <Card key={s} size="small" title={`${t(`p4.st_${s}`)} (${rows.filter((r) => r.stage === s).length})`} style={{ minWidth: 220, flex: 1, background: '#fafafa' }}>
        {rows.filter((r) => r.stage === s).map((r) => <Card key={r.id} size="small" style={{ marginBottom: 8, borderColor: r.stuck_days ? '#ff4d4f' : undefined }}>
          <div><b>{r.name}</b> {r.is_qualified && <Tag color="green">{t('p4.qualified')}</Tag>}</div>
          <div>{r.currency} {r.amount.toLocaleString()} · {t('p4.winProb')} {r.win_probability}%</div>
          {r.stuck_days && <div style={{ color: '#cf1322' }}>{t('p4.stuck', { d: r.stuck_days })}</div>}
          {r.lost_reason && <Tag>{t(`p4.lr_${r.lost_reason}`)}</Tag>}
          {can('crm.manage') && r.stage !== 'won' && <Select size="small" style={{ width: '100%', marginTop: 6 }} placeholder={t('p4.moveTo')} value={null as any}
            onChange={(v) => move(r.id, v)} options={STAGES.filter((x) => x !== r.stage).map((x) => ({ value: x, label: t(`p4.st_${x}`) }))} />}
        </Card>)}
      </Card>)}
    </div>
    <Modal open={!!lost} title={t('p4.lostReason')} onCancel={() => setLost(null)} onOk={() => move(lost!.id, 'lost', lost!.reason)} okButtonProps={{ disabled: !lost?.reason }}>
      <Select style={{ width: '100%' }} value={lost?.reason} onChange={(v) => setLost({ ...lost!, reason: v })} options={LOST.map((x) => ({ value: x, label: t(`p4.lr_${x}`) }))} />
    </Modal>
    <Modal open={creating} title={t('p4.newOpp')} footer={null} onCancel={() => setCreating(false)} destroyOnClose>
      <Form layout="vertical" onFinish={async (v) => { await api.post('/opportunities', { ...v, expected_close_date: v.expected_close_date?.format('YYYY-MM-DD') }); setCreating(false); load() }}>
        <Form.Item name="account_id" label={t('p4.account')} rules={[{ required: true }]}><Select showSearch optionFilterProp="label" options={accounts.map((a) => ({ value: a.id, label: a.name }))} /></Form.Item>
        <Form.Item name="name" label={t('p4.oppName')} rules={[{ required: true }]}><Input /></Form.Item>
        <Space><Form.Item name="amount" label={t('p4.amount')} initialValue={0}><InputNumber min={0} /></Form.Item>
          <Form.Item name="currency" label={t('p4.currency')} initialValue="USD"><Input style={{ width: 80 }} maxLength={3} /></Form.Item>
          <Form.Item name="expected_close_date" label={t('p4.expectedClose')}><DatePicker /></Form.Item></Space>
        <Button type="primary" htmlType="submit">{t('p4.save')}</Button>
      </Form>
    </Modal>
  </>
}

function QuoteEditor({ quote, onDone }: { quote: any; onDone: () => void }) {
  const { t } = useTranslation()
  const [accounts, setAccounts] = useState<any[]>([])
  const [opps, setOpps] = useState<any[]>([])
  const [form] = Form.useForm()
  useEffect(() => { api.get('/accounts', { params: { size: 100 } }).then((r) => setAccounts(r.data.items)) }, [])
  const acc = Form.useWatch('account_id', form)
  useEffect(() => { if (acc) api.get('/opportunities', { params: { account_id: acc } }).then((r) => setOpps(r.data.items)) }, [acc])
  const items = Form.useWatch('items', form) || []
  const disc = Form.useWatch('discount_pct', form) || 0
  const sub = items.reduce((s: number, i: any) => s + (i?.qty || 0) * (i?.unit_price || 0), 0)
  const cost = items.reduce((s: number, i: any) => s + (i?.qty || 0) * (i?.unit_cost || 0), 0)
  const total = sub * (1 - disc / 100)
  const margin = total > 0 ? ((total - cost) / total) * 100 : 0
  const init = quote ? { ...quote, valid_until: undefined } : { incoterm: 'FOB', currency: 'USD', language: 'en', discount_pct: 0, items: [{ unit: 'pcs' }] }
  const submit = async (v: any) => {
    const body = { ...v, valid_until: v.valid_until?.format('YYYY-MM-DD') ?? quote?.valid_until ?? null }
    if (quote?.id) await api.put(`/quotes/${quote.id}`, body); else await api.post('/quotes', body)
    onDone()
  }
  return <Form form={form} layout="vertical" initialValues={init} onFinish={submit}>
    <Row gutter={8}>
      <Col span={12}><Form.Item name="account_id" label={t('p4.account')} rules={[{ required: true }]}><Select disabled={!!quote?.id} showSearch optionFilterProp="label" options={accounts.map((a) => ({ value: a.id, label: a.name }))} /></Form.Item></Col>
      <Col span={12}><Form.Item name="opportunity_id" label={t('p4.opp')}><Select allowClear options={opps.map((o) => ({ value: o.id, label: o.name }))} /></Form.Item></Col>
      <Col span={6}><Form.Item name="incoterm" label={t('p4.incoterm')}><Select options={INCOTERMS.map((x) => ({ value: x, label: x }))} /></Form.Item></Col>
      <Col span={6}><Form.Item name="port" label={t('p4.port')}><Input /></Form.Item></Col>
      <Col span={4}><Form.Item name="currency" label={t('p4.currency')}><Input maxLength={3} /></Form.Item></Col>
      <Col span={4}><Form.Item name="language" label={t('p4.docLang')}><Select options={[{ value: 'en', label: 'English' }, { value: 'zh', label: '中文' }]} /></Form.Item></Col>
      <Col span={4}><Form.Item name="valid_until" label={t('p4.validUntil')}><DatePicker /></Form.Item></Col>
    </Row>
    <Form.List name="items">{(fields, { add, remove }) => <>
      {fields.map((f) => <Space key={f.key} align="baseline" wrap>
        <Form.Item name={[f.name, 'name']} rules={[{ required: true }]}><Input placeholder={t('p4.itemName')} style={{ width: 180 }} /></Form.Item>
        <Form.Item name={[f.name, 'sku']}><Input placeholder="SKU" style={{ width: 90 }} /></Form.Item>
        <Form.Item name={[f.name, 'qty']} rules={[{ required: true }]}><InputNumber placeholder={t('p4.qty')} min={0} /></Form.Item>
        <Form.Item name={[f.name, 'unit']}><Input placeholder={t('p4.unit')} style={{ width: 60 }} /></Form.Item>
        <Form.Item name={[f.name, 'unit_price']} rules={[{ required: true }]}><InputNumber placeholder={t('p4.unitPrice')} min={0} precision={4} /></Form.Item>
        <Form.Item name={[f.name, 'unit_cost']}><InputNumber placeholder={t('p4.unitCost')} min={0} precision={4} /></Form.Item>
        <MinusCircleOutlined onClick={() => remove(f.name)} />
      </Space>)}
      <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ unit: 'pcs' })}>{t('p4.addItem')}</Button></>}
    </Form.List>
    <Row gutter={8} style={{ marginTop: 12 }}>
      <Col span={6}><Form.Item name="discount_pct" label={t('p4.discount')}><InputNumber min={0} max={100} addonAfter="%" /></Form.Item></Col>
      <Col span={6}><Form.Item name="freight" label={t('p4.freight')}><InputNumber min={0} /></Form.Item></Col>
      <Col span={6}><Form.Item name="insurance" label={t('p4.insurance')}><InputNumber min={0} /></Form.Item></Col>
      <Col span={6}><Form.Item name="commission" label={t('p4.commission')}><InputNumber min={0} /></Form.Item></Col>
      <Col span={12}><Form.Item name="payment_terms" label={t('p4.paymentTerms')}><Input /></Form.Item></Col>
      <Col span={12}><Form.Item name="delivery_terms" label={t('p4.deliveryTerms')}><Input /></Form.Item></Col>
    </Row>
    <Form.Item name="notes" label={t('p4.notes')}><Input.TextArea rows={2} /></Form.Item>
    <Space>
      <Typography.Text>{t('p4.total')}: {total.toFixed(2)}</Typography.Text>
      <Typography.Text type={margin < 15 || disc > 10 ? 'danger' : 'success'}>{t('p4.margin')}: {margin.toFixed(1)}%{(margin < 15 || disc > 10) && ` · ${t('p4.needsApproval')}`}</Typography.Text>
      <Button type="primary" htmlType="submit">{t('p4.save')}</Button>
    </Space>
  </Form>
}

function Quotes() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [edit, setEdit] = useState<any>(null)
  const [link, setLink] = useState<string>()
  const [voiding, setVoiding] = useState<{ id: string; reason?: string } | null>(null)
  const load = useCallback(async () => setRows((await api.get('/quotes', { params: { size: 100 } })).data.items), [])
  useEffect(() => { load() }, [load])
  const act = async (id: string, a: string, body?: any) => {
    const r = await api.post(`/quotes/${id}:${a}`, body)
    if (a === 'send') setLink(`${location.origin}${r.data.confirm_url}`)
    if (a === 'pi') message.success(`${t('p4.piCreated')} ${r.data.pi_no}`)
    load()
  }
  return <>
    {can('crm.manage') && <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setEdit({})}>{t('p4.newQuote')}</Button>}
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('p4.quoteNo'), render: (_: any, r: any) => `${r.quote_no} v${r.doc_version}` },
      { title: t('p4.incoterm'), dataIndex: 'incoterm' },
      { title: t('p4.total'), render: (_: any, r: any) => `${r.currency} ${r.total.toLocaleString()}` },
      ...(can('quote.approve') ? [{ title: t('p4.margin'), dataIndex: 'gross_margin_pct', render: (v: number) => v == null ? '—' : `${v}%` }] : []),
      { title: t('p3.status'), dataIndex: 'status', render: (v: string, r: any) => <Space size={2}><Tag color={QS_COLOR[v]}>{t(`p4.qs_${v}`)}</Tag>
        {r.approval_level && v === 'pending_approval' && <Tag>{t(`p4.al_${r.approval_level}`)}</Tag>}</Space> },
      { title: t('p4.validUntil'), dataIndex: 'valid_until' },
      { title: t('p4.actions'), render: (_: any, r: any) => <Space size={4} wrap>
        {['draft', 'rejected'].includes(r.status) && can('crm.manage') && <><Button size="small" onClick={() => setEdit(r)}>{t('p4.edit')}</Button><Button size="small" type="primary" onClick={() => act(r.id, 'submit')}>{t('p4.submit')}</Button></>}
        {r.status === 'pending_approval' && can('quote.approve') && <><Button size="small" type="primary" onClick={() => act(r.id, 'review', { approve: true })}>{t('p4.approve')}</Button>
          <Button size="small" danger onClick={() => act(r.id, 'review', { approve: false })}>{t('p4.rejectQ')}</Button></>}
        {['approved', 'sent'].includes(r.status) && can('crm.manage') && <Button size="small" onClick={() => act(r.id, 'send')}>{t('p4.sendLink')}</Button>}
        {r.status === 'locked' && can('crm.manage') && <Button size="small" type="primary" onClick={() => act(r.id, 'pi')}>{t('p4.makePi')}</Button>}
        {['approved', 'sent', 'locked', 'superseded'].includes(r.status) && can('crm.manage') && <Button size="small" onClick={() => act(r.id, 'new-version')}>{t('p4.newVersion')}</Button>}
        <Button size="small" onClick={() => download(`/quotes/${r.id}/pdf`, `${r.quote_no}-v${r.doc_version}.pdf`)}>PDF</Button>
        {!['void', 'converted'].includes(r.status) && can('crm.manage') && <Button size="small" danger onClick={() => setVoiding({ id: r.id })}>{t('p4.void')}</Button>}
      </Space> },
    ]} expandable={{ expandedRowRender: (r: any) => <>{r.approval_reasons?.length > 0 && <div>{t('p4.approvalReasons')}：{r.approval_reasons.join('；')}</div>}
      {r.confirmed_by && <div>{t('p4.confirmedBy')}：{r.confirmed_by} · {r.confirmed_at && new Date(r.confirmed_at).toLocaleString()}</div>}
      {r.customer_comment && <div>{t('p4.customerComment')}：{r.customer_comment}</div>}</> }} />
    <Modal open={!!edit} width={980} title={edit?.id ? t('p4.edit') : t('p4.newQuote')} footer={null} onCancel={() => setEdit(null)} destroyOnClose>
      {edit && <QuoteEditor quote={edit.id ? edit : null} onDone={() => { setEdit(null); load() }} />}
    </Modal>
    <Modal open={!!link} title={t('p4.confirmLink')} footer={null} onCancel={() => setLink(undefined)}>
      <Typography.Paragraph copyable>{link}</Typography.Paragraph>
      <Typography.Text type="secondary">{t('p4.confirmLinkHint')}</Typography.Text>
    </Modal>
    <Modal open={!!voiding} title={t('p4.void')} onCancel={() => setVoiding(null)} okButtonProps={{ disabled: !voiding?.reason?.trim() }}
      onOk={async () => { await act(voiding!.id, 'void', { reason: voiding!.reason }); setVoiding(null) }}>
      <Input.TextArea placeholder={t('p4.voidReason')} onChange={(e) => setVoiding({ ...voiding!, reason: e.target.value })} />
    </Modal>
  </>
}

function Pis() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [bank, setBank] = useState<any>()
  const load = useCallback(async () => {
    const [a, b] = await Promise.all([api.get('/pis'), api.get('/crm/settings/bank')]); setRows(a.data); setBank(b.data)
  }, [])
  useEffect(() => { load() }, [load])
  return <Row gutter={16}>
    <Col span={14}><Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('p4.piNo'), dataIndex: 'pi_no' }, { title: t('p4.total'), render: (_: any, r: any) => `${r.currency} ${r.total.toLocaleString()}` },
      { title: t('p4.createdAt'), dataIndex: 'created_at', render: (v: string) => new Date(v).toLocaleDateString() },
      { title: '', render: (_: any, r: any) => <Button size="small" onClick={() => download(`/pis/${r.id}/pdf`, `${r.pi_no}.pdf`)}>PDF</Button> },
    ]} /></Col>
    <Col span={10}><Card size="small" title={t('p4.bankInfo')}>
      {bank && <Form layout="vertical" initialValues={bank} key={JSON.stringify(bank)} disabled={!can('billing.manage')}
        onFinish={async (v) => { await api.put('/crm/settings/bank', v); message.success(t('p4.done')); load() }}>
        <Form.Item name="beneficiary" label={t('p4.beneficiary')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="bank_name" label={t('p4.bankName')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="account_no" label={t('p4.accountNo')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="swift" label="SWIFT"><Input /></Form.Item>
        <Form.Item name="bank_address" label={t('p4.bankAddress')}><Input /></Form.Item>
        {can('billing.manage') && <Button type="primary" htmlType="submit">{t('p4.save')}</Button>}
        {!can('billing.manage') && <Typography.Text type="secondary">{t('p4.bankOwnerOnly')}</Typography.Text>}
      </Form>}
    </Card>
    <QuoteThresholds /></Col>
  </Row>
}

function QuoteThresholds() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [v, setV] = useState<any>()
  useEffect(() => { api.get('/crm/settings/quote').then(r => setV(r.data)) }, [])
  return <Card size="small" title={t('p5.quoteTh')} style={{ marginTop: 12 }}>{v && <Form layout="vertical" initialValues={v} disabled={!can('billing.manage')}
    onFinish={async (x) => { await api.put('/crm/settings/quote', x); message.success(t('p4.done')) }}>
    <Form.Item name="margin_manager" label={t('p5.mm')}><InputNumber min={0} max={100} /></Form.Item>
    <Form.Item name="margin_owner" label={t('p5.mo')}><InputNumber min={0} max={100} /></Form.Item>
    <Form.Item name="discount_manager" label={t('p5.dm')}><InputNumber min={0} max={100} /></Form.Item>
    {can('billing.manage') && <Button type="primary" htmlType="submit">{t('p4.save')}</Button>}
  </Form>}</Card>
}

export default function CrmPage() {
  const { t } = useTranslation()
  return <Tabs items={[
    { key: 'a', label: t('p4.accounts'), children: <Accounts /> },
    { key: 'o', label: t('p4.pipeline'), children: <Pipeline /> },
    { key: 'q', label: t('p4.quotes'), children: <Quotes /> },
    { key: 'p', label: t('p4.pis'), children: <Pis /> },
  ]} />
}
