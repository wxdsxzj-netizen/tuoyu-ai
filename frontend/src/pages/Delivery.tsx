/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Button, Form, Input, Modal, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

export default function DeliveryPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [msgs, setMsgs] = useState<any[]>([])
  const [inb, setInb] = useState<any[]>([])
  const [view, setView] = useState<any>()
  const [logging, setLogging] = useState(false)
  const load = useCallback(async () => {
    const [a, b] = await Promise.all([api.get('/delivery/messages', { params: { size: 100 } }), api.get('/delivery/inbound', { params: { size: 100 } })])
    setMsgs(a.data.items); setInb(b.data.items)
  }, [])
  useEffect(() => { load() }, [load])
  return (
    <>
      {can('sequence.manage') && <Button style={{ marginBottom: 12 }} onClick={() => setLogging(true)}>{t('p3.logReply')}</Button>}
      <Tabs items={[
        { key: 'o', label: t('p3.outbound'), children: <Table rowKey="id" size="small" dataSource={msgs} columns={[
          { title: t('p3.to'), dataIndex: 'to' }, { title: t('p3.subjectCol'), dataIndex: 'subject', render: (v: string, r: any) => <a onClick={() => setView(r)}>{v}</a> },
          { title: t('p3.status'), dataIndex: 'status', render: (v: string) => <Tag color={v === 'sent' ? 'green' : 'red'}>{t(`p3.os_${v}`, { defaultValue: String(v) })}</Tag> },
          { title: t('p3.sentAt'), dataIndex: 'sent_at', render: (v: string) => v && new Date(v).toLocaleString() },
          { title: t('p3.qaScore'), render: (_: any, r: any) => r.snapshot?.qa_score ?? '—' },
        ]} /> },
        { key: 'i', label: t('p3.inbound'), children: <Table rowKey="id" size="small" dataSource={inb} columns={[
          { title: t('p3.from'), dataIndex: 'from' }, { title: t('p3.subjectCol'), dataIndex: 'subject' },
          { title: t('p3.category'), dataIndex: 'category', render: (v: string, r: any) => <Tag color={r.needs_human ? 'orange' : 'blue'}>{t(`p3.rc_${v}`, { defaultValue: String(v) })} {Math.round((r.confidence || 0) * 100)}%</Tag> },
          { title: t('p3.receivedAt'), dataIndex: 'received_at', render: (v: string) => new Date(v).toLocaleString() },
        ]} expandable={{ expandedRowRender: (r: any) => <pre style={{ whiteSpace: 'pre-wrap' }}>{r.body}</pre> }} /> },
      ]} />
      <Modal open={!!view} title={view?.subject} footer={null} width={700} onCancel={() => setView(undefined)}>
        <pre style={{ whiteSpace: 'pre-wrap' }}>{view?.body}</pre>
        <pre style={{ background: '#fafafa', fontSize: 12 }}>{JSON.stringify(view?.snapshot, null, 2)}</pre>
      </Modal>
      <Modal open={logging} title={t('p3.logReply')} footer={null} onCancel={() => setLogging(false)} destroyOnClose>
        <Form layout="vertical" onFinish={async (v) => {
          const r = await api.post('/delivery/inbound', v); message.info(`${t(`p3.rc_${r.data.category}`, { defaultValue: String(r.data.category) })} · ${r.data.action}`); setLogging(false); load()
        }}>
          <Form.Item name="from_addr" label={t('p3.from')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="subject" label={t('p3.subjectCol')}><Input /></Form.Item>
          <Form.Item name="body" label={t('p3.body')} rules={[{ required: true }]}><Input.TextArea rows={5} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('p3.submit')}</Button>
        </Form>
      </Modal>
    </>
  )
}
