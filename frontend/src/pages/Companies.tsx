import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Checkbox, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import axios from 'axios'
import { api } from '../api/client'
import { useAuth } from '../auth'
import { ContactsTable } from './Contacts'

export interface Company { id: string; name: string; domain: string | null; country: string | null; city: string | null; industry: string | null; employee_range: string | null; description: string | null; phone: string | null; is_won_customer: boolean; source: string; contact_count: number; version: number }
interface Facet { value: string; count: number }

export default function CompaniesPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<Company[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [f, setF] = useState<{ q?: string; country?: string[]; industry?: string; employee_range?: string; is_won_customer?: boolean; has_contacts?: boolean; page: number }>({ page: 1 })
  const [facets, setFacets] = useState<Record<string, Facet[]>>({})
  const [detail, setDetail] = useState<Company | null>(null)
  const [adding, setAdding] = useState(false)
  const [reload, setReload] = useState(0)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.get('/companies', { params: { ...f, size: 20 }, paramsSerializer: { indexes: null } })
      setRows(r.data.items); setTotal(r.data.total)
    } finally { setLoading(false) }
  }, [f])
  useEffect(() => { load() }, [load])
  useEffect(() => { api.get('/companies/facets').then((r) => setFacets(r.data)) }, [reload])

  const create = async (force = false) => {
    const v = await form.validateFields()
    try {
      await api.post('/companies', v, { params: force ? { force: true } : {} })
      setAdding(false); form.resetFields(); load(); setReload(reload + 1)
    } catch (e) {
      if (axios.isAxiosError(e) && e.response?.data?.error?.details?.suspect_id) {
        Modal.confirm({ title: t('company.dupTitle'), content: e.response.data.error.details.reason, okText: t('company.force'), onOk: () => create(true) })
      }
    }
  }
  const enrich = async (c: Company) => {
    const r = await api.post(`/companies/${c.id}:enrich-contacts`, { limit: 10 })
    message.success(String(t('company.enrichDone', r.data)))
    const fresh = (await api.get(`/companies/${c.id}`)).data
    setDetail(fresh); load()
  }
  const opts = (k: string) => (facets[k] || []).map((x) => ({ value: x.value, label: `${x.value} (${x.count})` }))

  return (
    <Card title={t('menu.companies')} extra={can('company.manage') && <Button type="primary" onClick={() => setAdding(true)}>{t('company.add')}</Button>}>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search placeholder={t('company.search')} allowClear style={{ width: 260 }} onSearch={(q) => setF({ ...f, q: q || undefined, page: 1 })} />
        <Select mode="multiple" allowClear placeholder={t('company.country')} style={{ minWidth: 160 }} options={opts('country')} onChange={(v) => setF({ ...f, country: v.length ? v : undefined, page: 1 })} />
        <Select allowClear showSearch placeholder={t('company.industry')} style={{ width: 160 }} options={opts('industry')} onChange={(v) => setF({ ...f, industry: v, page: 1 })} />
        <Select allowClear placeholder={t('company.size')} style={{ width: 130 }} options={opts('employee_range')} onChange={(v) => setF({ ...f, employee_range: v, page: 1 })} />
        <Checkbox onChange={(e) => setF({ ...f, is_won_customer: e.target.checked || undefined, page: 1 })}>{t('company.onlyWon')}</Checkbox>
        <Checkbox onChange={(e) => setF({ ...f, has_contacts: e.target.checked || undefined, page: 1 })}>{t('company.hasContacts')}</Checkbox>
      </Space>
      <Table rowKey="id" loading={loading} dataSource={rows} onRow={(r) => ({ onClick: () => setDetail(r), style: { cursor: 'pointer' } })}
        pagination={{ current: f.page, total, pageSize: 20, onChange: (p) => setF({ ...f, page: p }), showTotal: (n) => `共 ${n} 家` }}
        columns={[
          { title: t('company.name'), dataIndex: 'name', render: (v, r) => <Space>{v}{r.is_won_customer && <Tag color="gold">{t('company.won')}</Tag>}</Space> },
          { title: t('company.domain'), dataIndex: 'domain' },
          { title: t('company.country'), dataIndex: 'country', width: 90 },
          { title: t('company.industry'), dataIndex: 'industry' },
          { title: t('company.size'), dataIndex: 'employee_range', width: 100 },
          { title: t('company.contacts'), dataIndex: 'contact_count', width: 80 },
          { title: t('company.source'), dataIndex: 'source', width: 90, render: (v) => <Tag>{v}</Tag> },
        ]} />
      <Drawer open={!!detail} width={720} title={detail?.name} onClose={() => setDetail(null)} destroyOnClose
        extra={detail && can('contact.enrich') && <Button type="primary" disabled={!detail.domain} onClick={() => enrich(detail)}>{t('company.enrich')}</Button>}>
        {detail && <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label={t('company.domain')}>{detail.domain || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('company.country')}>{detail.country || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('company.industry')}>{detail.industry || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('company.size')}>{detail.employee_range || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('contact.phone')}>{detail.phone || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('company.source')}>{detail.source}</Descriptions.Item>
          </Descriptions>
          <div style={{ marginTop: 16 }}><ContactsTable companyId={detail.id} key={detail.contact_count} /></div>
        </>}
      </Drawer>
      <Modal open={adding} title={t('company.add')} onOk={() => create()} onCancel={() => setAdding(false)} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('company.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="domain" label={t('company.domain')}><Input placeholder="example.com" /></Form.Item>
          <Form.Item name="country" label={t('company.country')}><Input placeholder="DE / 德国" /></Form.Item>
          <Form.Item name="industry" label={t('company.industry')}><Input /></Form.Item>
          <Form.Item name="employee_range" label={t('company.size')}><Input placeholder="20-200" /></Form.Item>
          <Form.Item name="phone" label={t('contact.phone')}><Input /></Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
