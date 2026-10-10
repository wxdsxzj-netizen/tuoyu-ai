/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Select, Table, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

export default function RecyclePage() {
  const { t } = useTranslation()
  const [kind, setKind] = useState<string>()
  const [rows, setRows] = useState<any[]>([])
  const load = () => { api.get('/recycle-bin', { params: { kind } }).then(r => setRows(r.data)) }
  useEffect(load, [kind])
  const restore = async (r: any) => { await api.post(`/recycle-bin/${r.kind}/${r.id}:restore`); message.success(t('b1.restored')); load() }
  return <>
    <Alert type="info" showIcon message={t('b1.recycleHint')} style={{ marginBottom: 12 }} />
    <Select allowClear placeholder={t('b1.type')} style={{ width: 180, marginBottom: 8 }} value={kind} onChange={setKind}
      options={['company', 'contact', 'account', 'opportunity', 'sequence'].map(v => ({ value: v, label: t('b1.k_' + v) }))} />
    <Table rowKey="id" size="small" dataSource={rows} columns={[
      { title: t('b1.type'), dataIndex: 'kind', render: v => <Tag>{t('b1.k_' + v)}</Tag> },
      { title: t('b1.name'), dataIndex: 'name' },
      { title: t('b1.deletedAt'), dataIndex: 'deleted_at', render: v => new Date(v).toLocaleString() },
      { title: t('b1.purgeAt'), dataIndex: 'purge_at', render: v => new Date(v).toLocaleDateString() },
      { title: '', render: (_, r: any) => <Button size="small" onClick={() => restore(r)}>{t('b1.restore')}</Button> },
    ]} />
  </>
}
