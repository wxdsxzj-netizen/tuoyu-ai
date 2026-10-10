import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Input, Select, Space, Table, Tag, Tooltip, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'
import { ComplianceCheckModal } from './Compliance'

const VS_COLOR: Record<string, string> = { valid: 'green', catch_all: 'gold', risky: 'orange', invalid: 'red', unknown: 'default', unverified: 'default' }

interface Contact { id: string; full_name: string; title: string | null; email: string | null; email_status: string; email_reason_codes: string[]; sanction_status: string; phone: string | null; company_name: string | null; country: string | null; source: string }

export function ContactsTable({ companyId }: { companyId?: string }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [checking, setChecking] = useState<string>()
  const [rows, setRows] = useState<Contact[]>([])
  const [total, setTotal] = useState(0)
  const [f, setF] = useState<{ q?: string; has_email?: boolean; email_status?: string; page: number }>({ page: 1 })
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
        <Select allowClear placeholder={t('contact.emailStatus')} style={{ width: 130 }} onChange={(v) => setF({ ...f, email_status: v, page: 1 })}
          options={['valid', 'catch_all', 'risky', 'invalid', 'unknown', 'unverified'].map((s) => ({ value: s, label: t(`vs.${s}`) }))} />
        {can('contact.verify') && !companyId && <Button onClick={async () => {
          const r = await api.post('/contacts:verify', { only_unverified: true }); message.success(String(t('cp.verifyQueued', { count: r.data.count }))); setTimeout(load, 1500)
        }}>{t('cp.verifyAll')}</Button>}
      </Space>
      <Table rowKey="id" size="small" dataSource={rows} pagination={{ current: f.page, total, pageSize: 20, onChange: (p) => setF({ ...f, page: p }) }}
        columns={[
          { title: t('contact.name'), dataIndex: 'full_name' },
          { title: t('contact.title'), dataIndex: 'title' },
          { title: t('contact.email'), dataIndex: 'email' },
          { title: <Tooltip title={t('cp.emailStatusTip')}>{t('contact.emailStatus')}</Tooltip>, dataIndex: 'email_status',
            render: (v: string, r: Contact) => <Tooltip title={(r.email_reason_codes || []).join(', ')}><Tag color={VS_COLOR[v]}>{t(`vs.${v}`)}</Tag></Tooltip> },
          ...(companyId ? [] : [{ title: t('contact.company'), dataIndex: 'company_name' }]),
          { title: t('company.source'), dataIndex: 'source', render: (v: string) => <Tag>{v}</Tag> },
          { title: '', render: (_: unknown, r: Contact) => <Space>
            {can('contact.verify') && r.email && <Button size="small" onClick={async () => { await api.post(`/contacts/${r.id}:verify`); load() }}>{t('cp.verify')}</Button>}
            {can('compliance.read') && <Button size="small" onClick={() => setChecking(r.id)}>{t('cp.check')}</Button>}</Space> },
        ]} />
      <ComplianceCheckModal contactId={checking} onClose={() => { setChecking(undefined); load() }} />
    </>
  )
}

export default function ContactsPage() {
  const { t } = useTranslation()
  return <Card title={t('menu.contacts')}><ContactsTable /></Card>
}
