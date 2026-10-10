import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Table, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
import dayjs from 'dayjs'
import { api } from '../api/client'

interface Row { id: string; actor_type: string; actor_id: string | null; action: string; object_type: string | null; object_id: string | null; before: object | null; after: object | null; result: string; ip: string | null; occurred_at: string }

export default function AuditPage() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const load = useCallback(async () => {
    const r = await api.get('/audit-logs', { params: { page, size: 50 } })
    setRows(r.data.items); setTotal(r.data.total)
  }, [page])
  useEffect(() => { load() }, [load])
  return (
    <Card title={t('menu.audit')} extra={<Button onClick={load}>{t('common.refresh')}</Button>}>
      <Table rowKey="id" size="small" dataSource={rows} pagination={{ current: page, total, pageSize: 50, onChange: setPage }}
        columns={[
          { title: t('audit.time'), dataIndex: 'occurred_at', render: (v) => dayjs(v).format('YYYY-MM-DD HH:mm:ss') },
          { title: t('audit.actor'), render: (_, r) => `${r.actor_type}${r.actor_id ? ':' + r.actor_id.slice(0, 8) : ''}` },
          { title: t('audit.action'), dataIndex: 'action' },
          { title: t('audit.object'), render: (_, r) => r.object_type ? `${r.object_type}:${(r.object_id || '').slice(0, 8)}` : '-' },
          { title: t('audit.result'), dataIndex: 'result', render: (v) => <Tag color={v === 'success' ? 'green' : 'red'}>{v}</Tag> },
          { title: t('audit.ip'), dataIndex: 'ip' },
          { title: t('audit.detail'), render: (_, r) => r.after ? <code>{JSON.stringify(r.after)}</code> : '-' },
        ]} />
    </Card>
  )
}
