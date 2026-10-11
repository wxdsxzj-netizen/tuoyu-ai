import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Form, Input, InputNumber, Select, Space, Table, Tag, Tooltip, message } from 'antd'
import { useTranslation } from 'react-i18next'
import dayjs from 'dayjs'
import { api } from '../api/client'
import { useJobPolling } from '../api/useJobPolling'
import { useAuth } from '../auth'

interface Crawl { id: string; urls: string[]; status: string; pages_fetched: number; pages_skipped_robots: number; pages_failed: number; items_created: number; log: { method?: string } | null; error: string | null; created_at: string }
interface Item { id: string; type: string; title: string; content: string; source_ref: string | null; status: string; version: number }
const COLORS: Record<string, string> = { pending: 'orange', confirmed: 'green', rejected: 'default', succeeded: 'green', partial: 'gold', failed: 'red', running: 'blue', queued: 'blue' }

export default function KbPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const manage = can('kb.manage')
  const { items: crawls, reload: loadCrawls, error: pollError } = useJobPolling<Crawl>('/kb/crawls')
  const [items, setItems] = useState<Item[]>([])
  const [total, setTotal] = useState(0)
  const [filters, setFilters] = useState<{ type?: string; status?: string; page: number }>({ status: 'pending', page: 1 })
  const [sel, setSel] = useState<string[]>([])
  const previousActive = useRef(false)
  const loadItems = useCallback(async () => {
    const r = await api.get('/kb/items', { params: { ...filters, size: 50 } })
    setItems(r.data.items); setTotal(r.data.total); setSel([])
  }, [filters])
  useEffect(() => { loadItems() }, [loadItems])
  useEffect(() => {
    const active = crawls.some(c => ['queued', 'running'].includes(c.status))
    if (previousActive.current && !active) loadItems()
    previousActive.current = active
  }, [crawls, loadItems])
  const start = async (v: { urls: string; rights_confirmed: boolean; max_pages: number }) => {
    const urls = v.urls.split(/\s+/).map((s) => s.trim()).filter(Boolean)
    await api.post('/kb/crawls', { urls, rights_confirmed: !!v.rights_confirmed, max_pages: v.max_pages })
    message.success(t('kb.start')); loadCrawls()
  }
  const batch = async (ids: string[], action: 'confirm' | 'reject') => {
    await api.post('/kb/items:batch', { ids, action }); loadItems()
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      {manage && <Card title={t('kb.crawl')}>
        <Form layout="vertical" onFinish={start} initialValues={{ max_pages: 30 }}>
          <Form.Item name="urls" label={t('kb.urls')} rules={[{ required: true }]}><Input.TextArea rows={2} placeholder="https://www.example.com" /></Form.Item>
          <Space align="start" wrap>
            <Form.Item name="max_pages" label={t('kb.maxPages')}><InputNumber min={1} max={200} /></Form.Item>
            <Form.Item name="rights_confirmed" valuePropName="checked" rules={[{ validator: (_, v) => v ? Promise.resolve() : Promise.reject(t('kb.rights')) }]} style={{ marginTop: 30 }}>
              <Checkbox>{t('kb.rights')}</Checkbox>
            </Form.Item>
          </Space>
          <Button type="primary" htmlType="submit">{t('kb.start')}</Button>
        </Form>
      </Card>}
      <Card title={t('kb.history')} size="small">
        {pollError && <Alert type="warning" showIcon message="任务状态刷新失败，正在重试。" />}
        <Table rowKey="id" size="small" dataSource={crawls} pagination={false} columns={[
          { title: 'URL', dataIndex: 'urls', render: (v: string[]) => v.join(', ') },
          { title: t('kb.status'), dataIndex: 'status', render: (v) => <Tag color={COLORS[v]}>{t(`status.${v}`)}</Tag> },
          { title: t('kb.pages'), dataIndex: 'pages_fetched' },
          { title: t('kb.robots'), dataIndex: 'pages_skipped_robots' },
          { title: t('kb.failed'), dataIndex: 'pages_failed' },
          { title: t('kb.created'), dataIndex: 'items_created' },
          { title: t('kb.method'), render: (_, r) => r.log?.method === 'llm' ? 'AI' : r.log?.method ? '规则' : '-' },
          { title: '', dataIndex: 'created_at', render: (v) => dayjs(v).format('MM-DD HH:mm') },
        ]} />
      </Card>
      <Card title={t('kb.items')} extra={<Space>
        <Select allowClear placeholder={t('kb.type')} style={{ width: 120 }} value={filters.type}
          options={['company_profile', 'product', 'certification', 'case', 'contact', 'selling_point', 'faq'].map((k) => ({ value: k, label: t(`kbType.${k}`) }))}
          onChange={(v) => setFilters({ ...filters, type: v, page: 1 })} />
        <Select allowClear placeholder={t('kb.status')} style={{ width: 110 }} value={filters.status}
          options={['pending', 'confirmed', 'rejected'].map((k) => ({ value: k, label: t(`status.${k}`) }))}
          onChange={(v) => setFilters({ ...filters, status: v, page: 1 })} />
        {manage && <Button disabled={!sel.length} onClick={() => batch(sel, 'confirm')}>{t('kb.batchConfirm')}</Button>}
        {manage && <Button disabled={!sel.length} danger onClick={() => batch(sel, 'reject')}>{t('kb.batchReject')}</Button>}
      </Space>}>
        <Table rowKey="id" size="small" dataSource={items}
          rowSelection={manage ? { selectedRowKeys: sel, onChange: (k) => setSel(k as string[]) } : undefined}
          pagination={{ current: filters.page, total, pageSize: 50, onChange: (p) => setFilters({ ...filters, page: p }) }}
          columns={[
            { title: t('kb.type'), dataIndex: 'type', width: 100, render: (v) => <Tag>{t(`kbType.${v}`)}</Tag> },
            { title: t('kb.title'), dataIndex: 'title' },
            { title: t('kb.content'), dataIndex: 'content', ellipsis: true },
            { title: t('kb.source'), dataIndex: 'source_ref', width: 90, render: (v) => v ? <Tooltip title={v}><a href={v} target="_blank" rel="noreferrer">↗</a></Tooltip> : '-' },
            { title: t('kb.status'), dataIndex: 'status', width: 90, render: (v) => <Tag color={COLORS[v]}>{t(`status.${v}`)}</Tag> },
            ...(manage ? [{ title: '', key: 'a', width: 120, render: (_: unknown, r: Item) => r.status === 'pending' ? <Space>
              <a onClick={() => batch([r.id], 'confirm')}>{t('kb.confirm')}</a>
              <a style={{ color: '#cf1322' }} onClick={() => batch([r.id], 'reject')}>{t('kb.reject')}</a></Space> : null }] : []),
          ]} />
      </Card>
    </Space>
  )
}
