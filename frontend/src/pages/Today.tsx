/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Badge, Button, Card, Col, DatePicker, Form, Input, Modal, Row, Select, Statistic, Table, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'

export default function TodayPage() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [d, setD] = useState<any>()
  const [tasks, setTasks] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const load = () => {
    api.get('/today').then(r => setD(r.data))
    api.get('/tasks').then(r => setTasks(r.data))
  }
  useEffect(load, [])
  const done = async (id: string) => { await api.patch(`/tasks/${id}`, { status: 'done' }); load() }
  const add = async (v: any) => { await api.post('/tasks', { ...v, due_at: v.due_at?.toISOString() }); setOpen(false); load() }
  if (!d) return null
  const card = (k: string, title: string, link: string, extra?: number) => <Col span={4} key={k}>
    <Card size="small" hoverable onClick={() => nav(link)}>
      <Statistic title={title} value={d[k]?.count ?? (d[k]?.items?.length || 0)} suffix={extra ? <Tag color="red">{t('b1.overdue')} {extra}</Tag> : null} />
    </Card></Col>
  return <>
    <Row gutter={12} style={{ marginBottom: 12 }}>
      {card('approvals', t('b1.todayApprovals'), '/approvals-center', d.approvals?.overdue)}
      {card('handoffs', t('b1.todayHandoffs'), '/handoffs')}
      {card('inquiries', t('b1.todayInquiries'), '/inquiries', d.inquiries?.overdue)}
      {card('conversations', t('b1.todayConversations'), '/inbox')}
      {card('tasks', t('b1.todayTasks'), '/today')}
      {card('stuck_opportunities', t('b1.todayStuck'), '/crm')}
    </Row>
    <Card size="small" title={t('b1.tasks')} extra={<Button type="primary" onClick={() => setOpen(true)}>{t('b1.newTask')}</Button>}>
      <Table rowKey="id" size="small" dataSource={tasks} pagination={{ pageSize: 20 }} columns={[
        { title: t('b1.taskTitle'), dataIndex: 'title', render: (v, r: any) => <>{r.overdue ? <Badge status="error" /> : null} {v}</> },
        { title: t('b1.taskType'), dataIndex: 'task_type', render: v => t('b1.tt_' + v) },
        { title: t('b1.priority'), dataIndex: 'priority', render: v => <Tag color={v === 'high' ? 'red' : v === 'low' ? 'default' : 'blue'}>{t('b1.p_' + v)}</Tag> },
        { title: t('b1.dueAt'), dataIndex: 'due_at', render: (v, r: any) => v ? <span style={{ color: r.overdue ? 'red' : undefined }}>{new Date(v).toLocaleString()}</span> : '—' },
        { title: t('b1.source'), dataIndex: 'source', render: v => t('b1.src_' + v, { defaultValue: v }) },
        { title: '', render: (_, r: any) => <Button size="small" onClick={() => done(r.id)}>{t('b1.complete')}</Button> },
      ]} />
    </Card>
    <Modal open={open} title={t('b1.newTask')} footer={null} onCancel={() => setOpen(false)} destroyOnClose>
      <Form layout="vertical" onFinish={add} initialValues={{ task_type: 'custom', priority: 'normal' }}>
        <Form.Item name="title" label={t('b1.taskTitle')} rules={[{ required: true, max: 100 }]}><Input /></Form.Item>
        <Form.Item name="task_type" label={t('b1.taskType')}><Select options={['follow_up', 'call', 'linkedin', 'sample', 'custom'].map(v => ({ value: v, label: t('b1.tt_' + v) }))} /></Form.Item>
        <Form.Item name="priority" label={t('b1.priority')}><Select options={['low', 'normal', 'high'].map(v => ({ value: v, label: t('b1.p_' + v) }))} /></Form.Item>
        <Form.Item name="due_at" label={t('b1.dueAt')}><DatePicker showTime /></Form.Item>
        <Form.Item name="note" label={t('b1.note')}><Input.TextArea rows={3} /></Form.Item>
        <Button type="primary" htmlType="submit" block>{t('b1.save')}</Button>
      </Form>
    </Modal>
  </>
}
