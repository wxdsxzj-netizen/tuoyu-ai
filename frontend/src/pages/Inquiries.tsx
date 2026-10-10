/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Descriptions, Drawer, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const GC: Record<string, string> = { A: 'red', B: 'orange', C: 'blue', D: 'default' }
const STATUSES = ['pending_review', 'pending_assign', 'assigned', 'responded', 'converted', 'closed']

function Countdown({ due }: { due?: string }) {
  const { t } = useTranslation()
  if (!due) return <>—</>
  const m = Math.round((new Date(due).getTime() - Date.now()) / 60000)
  return <Tag color={m < 0 ? 'red' : m < 60 ? 'orange' : 'green'}>{m < 0 ? `超时 ${-m} 分钟` : `${t('p5.slaDue')} ${new Date(due).toLocaleString()}`}</Tag>
}

function InquiryList() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const can = (p: string) => me?.permissions?.includes(p)
  const [status, setStatus] = useState<string | undefined>()
  const [rows, setRows] = useState<any[]>([])
  const [view, setView] = useState<any>()
  const [users, setUsers] = useState<any[]>([])
  const load = useCallback(async () => setRows((await api.get('/inquiries', { params: { status, page_size: 100 } })).data.items), [status])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (can('inquiry.manage')) api.get('/users').then(r => setUsers(r.data.items ?? r.data)) }, [])  // eslint-disable-line
  const open = async (id: string) => setView((await api.get(`/inquiries/${id}`)).data)
  const act = async (path: string, body?: any) => { const r = await api.post(`/inquiries/${view.id}:${path}`, body); setView({ ...view, ...r.data }); load() }
  const Items = ({ list }: { list: any[] }) => <>{(list || []).map((x, i) => <div key={i}>{x.item} <b style={{ color: x.points < 0 ? '#cf1322' : undefined }}>{x.points > 0 ? '+' : ''}{x.points}</b></div>)}</>
  return <>
    <Space style={{ marginBottom: 12 }}>
      <Select allowClear placeholder={t('p5.status')} style={{ width: 180 }} value={status} onChange={setStatus}
        options={STATUSES.map(s => ({ value: s, label: t(`p5.st_${s}`) }))} />
      <Button onClick={load}>刷新</Button>
    </Space>
    <Table rowKey="id" dataSource={rows} size="middle" onRow={r => ({ onClick: () => open(r.id), style: { cursor: 'pointer' } })} columns={[
      { title: t('p5.grade'), dataIndex: 'grade', render: g => g ? <Tag color={GC[g]}>{g}</Tag> : '—' },
      { title: t('p5.score'), dataIndex: 'score', render: s => s ?? '—' },
      { title: t('p5.peer'), dataIndex: 'peer_email' },
      { title: t('p5.status'), dataIndex: 'status', render: s => t(`p5.st_${s}`) },
      { title: t('p5.assignee'), dataIndex: 'assignee_name', render: (n, r) => n ? `${n}（${t(`p5.by_${r.assigned_by}`)}）` : <Tag>{t('p5.pool')}</Tag> },
      { title: t('p5.sla'), render: (_, r) => r.status === 'assigned' ? <Countdown due={r.sla_due_at} /> : r.first_response_at ? new Date(r.first_response_at).toLocaleString() : '—' },
      { title: t('p5.risk'), dataIndex: 'risk_flags', render: (f: string[]) => (f || []).map(x => <Tag color="red" key={x}>{t(`p5.risk_${x}`)}</Tag>) },
    ]} />
    <Drawer open={!!view} width={640} onClose={() => setView(undefined)} title={view?.peer_email}>
      {view && <>
        {(view.risk_flags || []).length > 0 && <Card size="small" style={{ background: '#fff1f0', borderColor: '#ffa39e', marginBottom: 12 }}>
          {(view.risk_flags || []).map((x: string) => <div key={x}><b>⚠ {t(`p5.risk_${x}`)}</b></div>)}
          <div>{t('p5.high_risk')}</div></Card>}
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label={t('p5.grade')}><Tag color={GC[view.grade]}>{view.grade ?? '—'}</Tag> {view.score}</Descriptions.Item>
          <Descriptions.Item label={t('p5.status')}>{t(`p5.st_${view.status}`)}</Descriptions.Item>
          <Descriptions.Item label={t('p5.auth')}>{view.authenticity_score}</Descriptions.Item>
          <Descriptions.Item label={t('p5.clarity')}>{view.clarity_score}</Descriptions.Item>
          <Descriptions.Item label={t('p5.fit')}>{view.fit_score}</Descriptions.Item>
          <Descriptions.Item label={t('p5.sla')}>{view.sla_minutes ? `${view.sla_minutes} 工作分钟` : '—'}</Descriptions.Item>
          <Descriptions.Item label={t('p5.assignee')} span={2}>{view.assignee_name ?? t('p5.pool')}</Descriptions.Item>
        </Descriptions>
        {view.score_detail && <Card size="small" title={t('p5.detail')} style={{ marginTop: 12 }}>
          <Typography.Text type="secondary">{t('p5.formula')}：{view.score_detail.formula}</Typography.Text>
          <Descriptions column={1} size="small">
            <Descriptions.Item label={t('p5.auth')}><Items list={view.score_detail.authenticity} /></Descriptions.Item>
            <Descriptions.Item label={t('p5.clarity')}><Items list={view.score_detail.clarity} /></Descriptions.Item>
            <Descriptions.Item label={t('p5.fit')}><Items list={view.score_detail.fit} /></Descriptions.Item>
          </Descriptions></Card>}
        <Space wrap style={{ marginTop: 12 }}>
          {view.status === 'pending_review' && can('inquiry.manage') && <>
            <Button type="primary" onClick={() => act('review', { genuine: true })}>{t('p5.genuine')}</Button>
            <Button danger onClick={() => act('review', { genuine: false, reason: 'invalid' })}>{t('p5.invalid')}</Button></>}
          {view.status === 'pending_assign' && (view.assigned_by === 'grab'
            ? <Button type="primary" onClick={() => act('grab', {})}>{t('b1.grab')}</Button>
            : <Button type="primary" onClick={() => act('assign', { assignee_id: me?.user.id })}>{t('p5.claim')}</Button>)}
          {can('inquiry.manage') && ['assigned', 'pending_assign'].includes(view.status) &&
            <Select placeholder={t('p5.reassign')} style={{ width: 180 }} onChange={v => act('assign', { assignee_id: v })}
              options={users.map(u => ({ value: u.id, label: u.name }))} />}
          {can('inquiry.manage') && <Button onClick={() => act('rescore')}>{t('p5.rescore')}</Button>}
          <Button href={`/inbox?c=${view.conversation_id}`}>打开会话</Button>
        </Space>
      </>}
    </Drawer>
  </>
}

function Rules() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const can = me?.permissions?.includes('inquiry.manage')
  const [rows, setRows] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [edit, setEdit] = useState<any>()
  const [tz, setTz] = useState('Asia/Shanghai')
  const [form] = Form.useForm()
  const load = async () => setRows((await api.get('/assignment-rules')).data)
  useEffect(() => { load(); api.get('/users').then(r => setUsers(r.data.items ?? r.data)).catch(() => null) }, [])
  const save = async () => {
    const v = await form.validateFields()
    const body = { ...v, countries: v.countries || [], languages: v.languages || [], grades: v.grades || [], keywords: v.keywords || [] }
    if (edit?.id) await api.put(`/assignment-rules/${edit.id}`, body); else await api.post('/assignment-rules', body)
    setEdit(undefined); load()
  }
  const name = (id: string) => users.find(u => u.id === id)?.name ?? id.slice(0, 8)
  return <>
    {can && <Space style={{ marginBottom: 12 }}>
      <Button type="primary" onClick={() => { form.resetFields(); form.setFieldsValue({ priority: 100 }); setEdit({}) }}>{t('p5.newRule')}</Button>
      <Input style={{ width: 200 }} value={tz} onChange={e => setTz(e.target.value)} addonBefore={t('p5.tz')} />
      <Button onClick={() => api.put('/inquiries/settings/timezone', { business_tz: tz })}>{t('p5.save')}</Button>
      <Select style={{ width: 160 }} placeholder={t('b1.grabMode')} onChange={v => api.put('/inquiries/settings/grab-mode', { enabled: v === 'on' })}
        options={[{ value: 'on', label: t('b1.grabOn') }, { value: 'off', label: t('b1.grabOff') }]} />
    </Space>}
    <Table rowKey="id" dataSource={rows} size="small" columns={[
      { title: t('p5.priority'), dataIndex: 'priority' }, { title: t('p5.ruleName'), dataIndex: 'name' },
      { title: t('p5.countries'), dataIndex: 'countries', render: v => v.join(', ') || '全部' },
      { title: t('p5.grades'), dataIndex: 'grades', render: v => v.join(', ') || '全部' },
      { title: t('p5.keywords'), dataIndex: 'keywords', render: v => v.join(', ') || '—' },
      { title: t('p5.members'), dataIndex: 'member_ids', render: (v: string[]) => v.map(name).join('、') },
      { title: '', render: (_, r) => can && <Space>
        <a onClick={() => { form.setFieldsValue(r); setEdit(r) }}>编辑</a>
        <Popconfirm title="确认删除？" onConfirm={async () => { await api.delete(`/assignment-rules/${r.id}`); load() }}><a>{t('p5.delete')}</a></Popconfirm></Space> },
    ]} />
    <Typography.Paragraph type="secondary">分配顺序：老客户归属人 → 按优先级匹配规则 → 规则成员中负载最小者（并列轮转）→ 无人可用进共享池并提醒经理。A/B/C 级响应时限 1 / 4 / 24 个工作小时；D 级需人工复核。</Typography.Paragraph>
    <Modal open={!!edit} onCancel={() => setEdit(undefined)} onOk={save} title={t('p5.newRule')}>
      <Form form={form} layout="vertical">
        <Form.Item name="name" label={t('p5.ruleName')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="priority" label={t('p5.priority')}><InputNumber min={1} /></Form.Item>
        <Form.Item name="countries" label={t('p5.countries')}><Select mode="tags" /></Form.Item>
        <Form.Item name="languages" label={t('p5.languages')}><Select mode="tags" /></Form.Item>
        <Form.Item name="grades" label={t('p5.grades')}><Select mode="multiple" options={['A', 'B', 'C', 'D'].map(g => ({ value: g, label: g }))} /></Form.Item>
        <Form.Item name="keywords" label={t('p5.keywords')}><Select mode="tags" /></Form.Item>
        <Form.Item name="member_ids" label={t('p5.members')} rules={[{ required: true }]}><Select mode="multiple" options={users.map(u => ({ value: u.id, label: u.name }))} /></Form.Item>
        <Form.Item name="daily_cap" label={t('p5.dailyCap')}><InputNumber min={1} /></Form.Item>
      </Form>
    </Modal>
  </>
}

export default function InquiriesPage() {
  const { t } = useTranslation()
  return <Tabs items={[{ key: 'l', label: t('menu.inquiries'), children: <InquiryList /> }, { key: 'r', label: t('p5.rules'), children: <Rules /> }]} />
}
