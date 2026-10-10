/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const DECISION_COLOR: Record<string, string> = { allow: 'green', approval: 'gold', consent_required: 'orange', deny: 'red' }

function Overview() {
  const { t } = useTranslation()
  const [d, setD] = useState<any>()
  useEffect(() => { api.get('/compliance/overview').then((r) => setD(r.data)) }, [])
  if (!d) return null
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {d.sanctions.stale && <Alert type="warning" showIcon message={t('cp.listStale')} />}
      {!d.lia_active && <Alert type="warning" showIcon message={t('cp.liaMissing')} />}
      {!d.settings_complete && <Alert type="warning" showIcon message={t('cp.settingsIncomplete')} />}
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label={t('cp.verifier')}>{d.verifier}</Descriptions.Item>
        <Descriptions.Item label={t('cp.sanctionPending')}>{d.sanction_pending}</Descriptions.Item>
        <Descriptions.Item label={t('cp.verification')}>
          {Object.entries(d.verification).map(([k, v]) => <Tag key={k}>{t(`vs.${k}`)}：{String(v)}</Tag>)}
        </Descriptions.Item>
        <Descriptions.Item label={t('cp.suppressions')}>
          {Object.entries(d.suppressions).map(([k, v]) => <Tag key={k}>{t(`cp.r_${k}`)}：{String(v)}</Tag>)}
        </Descriptions.Item>
        <Descriptions.Item label={t('cp.listUpdated')} span={2}>
          {d.sanctions.sources.map((s: any) => <Tag key={s.source}>{s.source.toUpperCase()} · {s.count} 条 · {new Date(s.fetched_at).toLocaleString()}</Tag>)}
          {!d.sanctions.sources.length && '—'}
        </Descriptions.Item>
      </Descriptions>
    </Space>
  )
}

function Suppressions() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [f, setF] = useState<any>({ page: 1 })
  const [adding, setAdding] = useState(false)
  const [revoking, setRevoking] = useState<any>()
  const load = useCallback(async () => {
    const r = await api.get('/suppressions', { params: { ...f, size: 20 } }); setRows(r.data.items); setTotal(r.data.total)
  }, [f])
  useEffect(() => { load() }, [load])
  const exportCsv = async () => {
    const r = await api.get('/suppressions:export', { responseType: 'blob' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(r.data); a.download = 'suppressions.csv'; a.click()
  }
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Input.Search placeholder={t('cp.value')} allowClear onSearch={(q) => setF({ ...f, q: q || undefined, page: 1 })} />
        <Select allowClear placeholder={t('cp.reason')} style={{ width: 140 }} onChange={(v) => setF({ ...f, reason: v, page: 1 })}
          options={['unsubscribe', 'complaint', 'hard_bounce', 'manual', 'dsr', 'compliance'].map((r) => ({ value: r, label: t(`cp.r_${r}`) }))} />
        {can('suppression.add') && <Button type="primary" onClick={() => setAdding(true)}>{t('cp.add')}</Button>}
        {can('compliance.manage') && <Button onClick={exportCsv}>{t('cp.export')}</Button>}
      </Space>
      <Table rowKey="id" size="small" dataSource={rows} pagination={{ current: f.page, total, pageSize: 20, onChange: (p) => setF({ ...f, page: p }) }}
        columns={[
          { title: t('cp.keyType'), dataIndex: 'key_type', render: (v) => t(`cp.k_${v}`) },
          { title: t('cp.value'), dataIndex: 'value', render: (v) => v || <i>{t('cp.hashOnly')}</i> },
          { title: t('cp.reason'), dataIndex: 'reason', render: (v) => <Tag color={['unsubscribe', 'complaint', 'dsr'].includes(v) ? 'red' : 'default'}>{t(`cp.r_${v}`)}</Tag> },
          { title: t('cp.channels'), dataIndex: 'channels', render: (v: string[]) => v.join(', ') },
          { title: t('cp.createdAt'), dataIndex: 'created_at', render: (v) => new Date(v).toLocaleString() },
          ...(can('compliance.manage') ? [{ title: '', render: (_: any, r: any) => <Button size="small" onClick={() => setRevoking(r)}>{t('cp.revoke')}</Button> }] : []),
        ]} />
      <Modal open={adding} title={t('cp.add')} onCancel={() => setAdding(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ key_type: 'email' }} onFinish={async (v) => { await api.post('/suppressions', v); setAdding(false); load() }}>
          <Form.Item name="key_type" label={t('cp.keyType')}><Select options={['email', 'domain', 'phone'].map((k) => ({ value: k, label: t(`cp.k_${k}`) }))} /></Form.Item>
          <Form.Item name="value" label={t('cp.value')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="note" label={t('cp.notes')}><Input /></Form.Item>
          <Button type="primary" htmlType="submit">{t('cp.save')}</Button>
        </Form>
      </Modal>
      <Modal open={!!revoking} title={t('cp.revoke')} onCancel={() => setRevoking(undefined)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ mode: 'consent' }} onFinish={async (v) => { await api.post(`/suppressions/${revoking.id}:revoke`, v); setRevoking(undefined); load() }}>
          <Form.Item name="mode"><Select options={[{ value: 'consent', label: t('cp.revokeConsent') }, { value: 'mistake', label: t('cp.revokeMistake') }]} /></Form.Item>
          <Form.Item name="evidence_ref" label={t('cp.evidence')}><Input /></Form.Item>
          <Button type="primary" danger htmlType="submit">{t('cp.revoke')}</Button>
        </Form>
      </Modal>
    </>
  )
}

function Sanctions() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [status, setStatus] = useState<string>('pending')
  const [search, setSearch] = useState<any>()
  const load = useCallback(async () => { const r = await api.get('/sanctions/hits', { params: { status, size: 100 } }); setRows(r.data.items) }, [status])
  useEffect(() => { load() }, [load])
  const review = async (id: string, decision: string) => { await api.post(`/sanctions/hits/${id}:review`, { decision }); load() }
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Select value={status} style={{ width: 130 }} onChange={setStatus}
          options={[{ value: 'pending', label: '待复核' }, { value: 'confirmed', label: '已确认' }, { value: 'false_positive', label: '误报' }]} />
        <Input.Search placeholder={t('cp.search')} onSearch={async (name) => name && setSearch((await api.get('/sanctions/search', { params: { name } })).data)} />
        {can('compliance.manage') && <Button onClick={async () => { await api.post('/sanctions:rescreen'); message.success(t('cp.ok')); load() }}>{t('cp.rescreen')}</Button>}
      </Space>
      {search && <Card size="small" style={{ marginBottom: 12 }} title={t('cp.search')} extra={<a onClick={() => setSearch(undefined)}>×</a>}>
        {search.stale && <Alert type="warning" message={t('cp.listStale')} />}
        <Table rowKey={(r: any) => r.name + r.list_name} size="small" pagination={false} dataSource={search.hits}
          columns={[{ title: t('cp.matched'), dataIndex: 'name' }, { title: t('cp.listName'), dataIndex: 'list_name' }, { title: t('cp.score'), dataIndex: 'score' }]} />
      </Card>}
      <Table rowKey="id" size="small" dataSource={rows} pagination={false}
        columns={[
          { title: t('cp.subject'), dataIndex: 'subject_name', render: (v, r: any) => <>{v} <Tag>{r.subject_type === 'company' ? '公司' : '联系人'}</Tag></> },
          { title: t('cp.matched'), dataIndex: 'entry_name' },
          { title: t('cp.listName'), dataIndex: 'list_name' },
          { title: t('cp.score'), dataIndex: 'score', render: (v) => <Tag color={v >= 0.95 ? 'red' : 'orange'}>{v.toFixed(2)}</Tag> },
          ...(can('compliance.manage') && status === 'pending' ? [{
            title: '', render: (_: any, r: any) => <Space>
              <Button size="small" danger onClick={() => review(r.id, 'confirmed')}>{t('cp.confirm')}</Button>
              <Button size="small" onClick={() => review(r.id, 'false_positive')}>{t('cp.falsePositive')}</Button></Space>,
          }] : []),
        ]} />
    </>
  )
}

function Lia() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [d, setD] = useState<any>({ items: [] })
  const [open, setOpen] = useState(false)
  const load = () => api.get('/compliance/lia').then((r) => setD(r.data))
  useEffect(() => { load() }, [])
  return (
    <>
      {can('compliance.manage') && <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setOpen(true)}>{t('cp.newLia')}</Button>}
      <Table rowKey="id" size="small" dataSource={d.items} pagination={false}
        columns={[
          { title: t('cp.purpose'), dataIndex: 'purpose' },
          { title: t('cp.balancing'), dataIndex: 'balancing_test', ellipsis: true },
          { title: t('cp.status'), dataIndex: 'id', render: (id) => id === d.active_id ? <Tag color="green">有效</Tag> : <Tag>已归档/过期</Tag> },
          { title: t('cp.reviewedAt'), dataIndex: 'reviewed_at', render: (v) => new Date(v).toLocaleDateString() },
          ...(can('compliance.manage') ? [{ title: '', render: (_: any, r: any) => <Button size="small" onClick={async () => { await api.post(`/compliance/lia/${r.id}:review`); load() }}>{t('cp.reviewLia')}</Button> }] : []),
        ]} />
      <Modal open={open} title={t('cp.newLia')} onCancel={() => setOpen(false)} footer={null} destroyOnClose width={640}>
        <Form layout="vertical" onFinish={async (v) => { await api.post('/compliance/lia', v); setOpen(false); load() }}
          initialValues={{ purpose: '向海外企业采购负责人推介本公司产品（B2B 营销）', data_sources: '企业官网、公开商业目录、展会名录', balancing_test: '仅处理公开的商务联系方式；与收件人职务直接相关；每封邮件提供一键退订；低频发送；收到反对立即停止。对个人影响极小，收件人可合理预期此类商务联系。' }}>
          <Form.Item name="purpose" label={t('cp.purpose')} rules={[{ required: true, min: 5 }]}><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="data_sources" label={t('cp.dataSources')}><Input /></Form.Item>
          <Form.Item name="balancing_test" label={t('cp.balancing')} rules={[{ required: true, min: 10 }]}><Input.TextArea rows={4} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('cp.save')}</Button>
        </Form>
      </Modal>
    </>
  )
}

function Consents() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const load = () => api.get('/compliance/consents', { params: { size: 100 } }).then((r) => setRows(r.data.items))
  useEffect(() => { load() }, [])
  return (
    <>
      <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setOpen(true)}>{t('cp.newConsent')}</Button>
      <Table rowKey="id" size="small" dataSource={rows} pagination={false}
        columns={[
          { title: t('cp.subject'), dataIndex: 'subject' }, { title: t('cp.basis'), dataIndex: 'basis' },
          { title: t('cp.evidenceType'), dataIndex: 'evidence_type' }, { title: t('cp.evidence'), dataIndex: 'evidence_ref' },
          { title: t('cp.status'), dataIndex: 'revoked_at', render: (v) => v ? <Tag>已撤回</Tag> : <Tag color="green">有效</Tag> },
          { title: '', render: (_: any, r: any) => !r.revoked_at && <Button size="small" onClick={async () => { await api.post(`/compliance/consents/${r.id}:revoke`); load() }}>{t('cp.revokeConsentBtn')}</Button> },
        ]} />
      <Modal open={open} title={t('cp.newConsent')} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ basis: 'consent', evidence_type: 'inquiry', channel: 'email_marketing' }}
          onFinish={async (v) => { await api.post('/compliance/consents', v); setOpen(false); load() }}>
          <Form.Item name="subject" label="邮箱 / 号码" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="basis" label={t('cp.basis')}><Select options={[['consent', '明示同意'], ['existing_customer', '现有客户'], ['contract', '合同'], ['legitimate_interest', '正当利益']].map(([value, label]) => ({ value, label }))} /></Form.Item>
          <Form.Item name="evidence_type" label={t('cp.evidenceType')}><Select options={[['form', '表单'], ['chat', '聊天'], ['inquiry', '询盘'], ['offline', '线下'], ['import', '导入']].map(([value, label]) => ({ value, label }))} /></Form.Item>
          <Form.Item name="evidence_ref" label={t('cp.evidence')}><Input /></Form.Item>
          <Button type="primary" htmlType="submit">{t('cp.save')}</Button>
        </Form>
      </Modal>
    </>
  )
}

function Rules() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  useEffect(() => { api.get('/compliance/region-rules').then((r) => setRows(r.data)) }, [])
  return (
    <Table rowKey="id" size="small" dataSource={rows} pagination={{ pageSize: 50 }}
      columns={[
        { title: t('cp.country'), dataIndex: 'country_code', render: (v) => v === '*' ? '默认' : v },
        { title: t('cp.subscriberType'), dataIndex: 'subscriber_type', render: (v) => ({ corporate: '法人', individual: '个人', unknown: '不确定' } as any)[v] },
        { title: t('cp.coldEmail'), dataIndex: 'cold_email', render: (v) => <Tag color={DECISION_COLOR[v]}>{t(`cp.${v}`)}</Tag> },
        { title: t('cp.lia'), dataIndex: 'requires_lia', render: (v) => v ? '是' : '' },
        { title: t('cp.required'), dataIndex: 'required_elements', render: (v: string[]) => v.map((x) => ({ address: '地址', unsubscribe: '退订', identity: '身份' } as any)[x]).join('、') },
        { title: t('cp.reason'), dataIndex: 'reason_code' },
        { title: t('cp.version'), dataIndex: 'version' },
        { title: t('cp.notes'), dataIndex: 'notes', ellipsis: true },
      ]} />
  )
}

function Settings() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [form] = Form.useForm()
  useEffect(() => { api.get('/compliance/settings').then((r) => form.setFieldsValue(r.data)) }, [form])
  return (
    <Form form={form} layout="vertical" style={{ maxWidth: 560 }} disabled={!can('compliance.manage')}
      onFinish={async (v) => { await api.put('/compliance/settings', v); message.success(t('cp.saved')) }}>
      <Form.Item name="sender_legal_name" label={t('cp.senderName')} rules={[{ required: true }]}><Input placeholder="Ningbo XX Valve Co., Ltd." /></Form.Item>
      <Form.Item name="postal_address" label={t('cp.address')} rules={[{ required: true }]}><Input.TextArea rows={2} /></Form.Item>
      <Form.Item name="privacy_email" label={t('cp.privacyEmail')}><Input /></Form.Item>
      <Form.Item name="footer_language" label={t('cp.footerLang')}><Select options={[{ value: 'en', label: 'English' }, { value: 'zh', label: '中文' }]} /></Form.Item>
      <Button type="primary" htmlType="submit">{t('cp.save')}</Button>
    </Form>
  )
}

export default function CompliancePage() {
  const { t } = useTranslation()
  return (
    <Card title={t('menu.compliance')}>
      <Tabs items={[
        { key: 'o', label: t('cp.overview'), children: <Overview /> },
        { key: 's', label: t('cp.suppressions'), children: <Suppressions /> },
        { key: 'x', label: t('cp.sanctions'), children: <Sanctions /> },
        { key: 'l', label: t('cp.lia'), children: <Lia /> },
        { key: 'c', label: t('cp.consents'), children: <Consents /> },
        { key: 'r', label: t('cp.rules'), children: <Rules /> },
        { key: 'g', label: t('cp.settings'), children: <Settings /> },
      ]} />
    </Card>
  )
}

export function ComplianceCheckModal({ contactId, onClose }: { contactId?: string; onClose: () => void }) {
  const { t } = useTranslation()
  const [d, setD] = useState<any>()
  useEffect(() => { setD(undefined); if (contactId) api.get(`/contacts/${contactId}/compliance-check`).then((r) => setD(r.data)) }, [contactId])
  return (
    <Modal open={!!contactId} title={t('cp.check')} onCancel={onClose} footer={null} width={620}>
      {d && <Descriptions column={1} bordered size="small">
        <Descriptions.Item label={t('cp.decision')}><Tag color={DECISION_COLOR[d.decision]}>{t(`cp.${d.decision}`)}</Tag></Descriptions.Item>
        <Descriptions.Item label={t('cp.blocks')}>{d.blocks.map((b: any) => <Tag color="red" key={b.code}>{b.code}{b.reasons ? `（${b.reasons.join(',')}）` : ''}{b.missing ? `（${b.missing.join(',')}）` : ''}</Tag>)}</Descriptions.Item>
        <Descriptions.Item label={t('cp.warnings')}>{[...d.warnings, ...d.needs].map((b: any) => <Tag color="gold" key={b.code}>{b.code}</Tag>)}</Descriptions.Item>
        <Descriptions.Item label={t('cp.region')}>{d.region.country || '未知'}（{d.region.country_source}）· {d.region.reason_codes.join(',')} · v{d.region.rule_version}</Descriptions.Item>
      </Descriptions>}
    </Modal>
  )
}
