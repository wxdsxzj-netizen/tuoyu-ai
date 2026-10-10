import { useCallback, useEffect, useState } from 'react'
import { Card, Input, Select, Space, Table, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

interface Contact { id: string; full_name: string; title: string | null; email: string | null; email_status: string; phone: string | null; company_name: string | null; country: string | null; source: string }

export function ContactsTable({ companyId }: { companyId?: string }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Contact[]>([])
  const [total, setTotal] = useState(0)
  const [f, setF] = useState<{ q?: string; has_email?: boolean; page: number }>({ page: 1 })
  const load = useCallback(async () => {
    const r = await api.get('/contacts', { params: { ...f, company_id: companyId, size: 20 } })
    setRows(r.data.items); setTotal(r.data.total)
  }, [f, companyId])
  useEffect(() => { load() }, [load])
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Input.Search placeholder={t('contact.search')} allowClear onSearch={(q) => setF({ ...f, q: q || undefined, page: 1 })} />
        <Select allowClear placeholder={t('contact.email')} style={{ width: 120 }} options={[{ value: 1, label: '有邮箱' }, { value: 0, label: '无邮箱' }]}
          onChange={(v) => setF({ ...f, has_email: v === undefined ? undefined : !!v, page: 1 })} />
      </Space>
      <Table rowKey="id" size="small" dataSource={rows} pagination={{ current: f.page, total, pageSize: 20, onChange: (p) => setF({ ...f, page: p }) }}
        columns={[
          { title: t('contact.name'), dataIndex: 'full_name' },
          { title: t('contact.title'), dataIndex: 'title' },
          { title: t('contact.email'), dataIndex: 'email' },
          { title: t('contact.emailStatus'), dataIndex: 'email_status', render: (v) => <Tag>{v === 'unverified' ? t('contact.unverified') : v}</Tag> },
          ...(companyId ? [] : [{ title: t('contact.company'), dataIndex: 'company_name' }]),
          { title: t('company.source'), dataIndex: 'source', render: (v: string) => <Tag>{v}</Tag> },
        ]} />
    </>
  )
}

export default function ContactsPage() {
  const { t } = useTranslation()
  return <Card title={t('menu.contacts')}><ContactsTable /></Card>
}
