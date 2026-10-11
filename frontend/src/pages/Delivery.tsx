/* eslint-disable @typescript-eslint/no-explicit-any */
import { useRef, useState } from 'react'
import { Alert, Button, Form, Input, Modal, Radio, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useServerPage, PageError } from '../useServerPage'
import { useAuth } from '../auth'

export default function DeliveryPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const outbound = useServerPage('/delivery/messages')
  const { rows: msgs } = outbound
  const inbound = useServerPage('/delivery/inbound')
  const { rows: inb } = inbound
  const [view, setView] = useState<any>()
  const [logging, setLogging] = useState(false)
  const [reconcile, setReconcile] = useState<any>()
  const [resolving, setResolving] = useState(false)
  const resolveLock = useRef(false)
  const canReconcile = can('sequence.approve')
  const intentList = useServerPage('/delivery/intents', {}, 'size', canReconcile)
  const { rows: intents, reload: loadIntents } = intentList
  const load = () => { outbound.reload(); inbound.reload() }
  return (
    <>
      {can('sequence.manage') && <Button style={{ marginBottom: 12 }} onClick={() => setLogging(true)}>{t('p3.logReply')}</Button>}
      <Tabs items={[
        ...(canReconcile ? [{ key: 'unknown', label: '待核查发送', children: <>
          <Alert type="warning" showIcon message="这些邮件可能已经发送。请先按 Message-ID 核查邮件服务日志，不要直接重发。" />
          <Button onClick={loadIntents} style={{ margin: '12px 0' }}>刷新核查队列</Button>
          <PageError page={intentList} /><Table rowKey="id" size="small" dataSource={intents}
            loading={intentList.loading} pagination={intentList.pagination} columns={[
              { title: 'Message-ID', dataIndex: 'message_id' },
              { title: '状态', dataIndex: 'state', render: (v: string) => v === 'sending' ? '发送中或结果尚未落库' : v === 'exhausted' ? '重试耗尽，须处理原因' : '结果未知' },
              { title: '最近尝试', dataIndex: 'updated_at', render: (v: string) => new Date(v).toLocaleString() },
              { title: '核查', render: (_: any, r: any) => <Button disabled={!r.can_reconcile} onClick={() => setReconcile(r)}>记录核查结论</Button> },
            ]} />
        </> }] : []),
        { key: 'o', label: t('p3.outbound'), children: <><PageError page={outbound} /><Table loading={outbound.loading} pagination={outbound.pagination} rowKey="id" size="small" dataSource={msgs} columns={[
          { title: t('p3.to'), dataIndex: 'to' }, { title: t('p3.subjectCol'), dataIndex: 'subject', render: (v: string, r: any) => <a onClick={() => setView(r)}>{v}</a> },
          { title: t('p3.status'), dataIndex: 'status', render: (v: string) => <Tag color={v === 'sent' ? 'green' : 'red'}>{t(`p3.os_${v}`, { defaultValue: String(v) })}</Tag> },
          { title: t('p3.sentAt'), dataIndex: 'sent_at', render: (v: string) => v && new Date(v).toLocaleString() },
          { title: t('p3.qaScore'), render: (_: any, r: any) => r.snapshot?.qa_score ?? '—' },
        ]} /></> },
        { key: 'i', label: t('p3.inbound'), children: <><PageError page={inbound} /><Table loading={inbound.loading} pagination={inbound.pagination} rowKey="id" size="small" dataSource={inb} columns={[
          { title: t('p3.from'), dataIndex: 'from' }, { title: t('p3.subjectCol'), dataIndex: 'subject' },
          { title: t('p3.category'), dataIndex: 'category', render: (v: string, r: any) => <Tag color={r.needs_human ? 'orange' : 'blue'}>{t(`p3.rc_${v}`, { defaultValue: String(v) })} {Math.round((r.confidence || 0) * 100)}%</Tag> },
          { title: t('p3.receivedAt'), dataIndex: 'received_at', render: (v: string) => new Date(v).toLocaleString() },
        ]} expandable={{ expandedRowRender: (r: any) => <pre style={{ whiteSpace: 'pre-wrap' }}>{r.body}</pre> }} /></> },
      ]} />
      <Modal open={!!reconcile} title="记录邮件服务核查结论" footer={null} onCancel={() => { if (!resolving) setReconcile(undefined) }} destroyOnClose>
        <p>{reconcile?.message_id}</p>
        <Alert type="warning" message="只有邮件服务明确证明未接收时，才能选择未发送。无法判断时请取消并保留待核查状态。" />
        <Form key={reconcile?.id} layout="vertical" onFinish={async (values) => {
          if (resolveLock.current) return
          resolveLock.current = true; setResolving(true)
          try {
            await api.post(`/delivery/intents/${reconcile.id}:reconcile`, values)
            message.success('核查结论已记录'); setReconcile(undefined); await Promise.all([loadIntents(), load()])
          } catch { /* Keep the evidence for correction/retry; the API client reports the error. */ }
          finally { resolveLock.current = false; setResolving(false) }
        }}>
          <Form.Item name="outcome" label="服务端核查结论" rules={[{ required: true }]}>
            <Radio.Group options={[{ label: '已接收，不重发', value: 'sent', disabled: reconcile?.state === 'exhausted' }, { label: '明确未接收，原因已处理，允许重试', value: 'not_sent' }]} />
          </Form.Item>
          <Form.Item name="evidence" label="日志时间、记录编号及核查依据" rules={[{ required: true }, { min: 10 }, { max: 2000 }]}>
            <Input.TextArea rows={4} disabled={resolving} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={resolving}>保存核查结论</Button>
        </Form>
      </Modal>
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
