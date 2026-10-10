/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Drawer, Input, InputNumber, Row, Select, Space, Statistic, Table, Tabs, Tag, Timeline, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const SC: Record<string, string> = { completed: 'green', partial: 'gold', failed: 'red', waiting_approval: 'orange', paused: 'purple', running: 'blue', cancelled: 'default' }

function TaskCard({ task, onChange }: { task: any; onChange: (t: any) => void }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [ans, setAns] = useState('')
  const act = async (a: string, body?: any) => {
    const r = await api.post(`/agent/tasks/${task.id}:${a}`, body)
    if (r.data.rollback) message.info(`${t('p5.rollbackDone')}：移出序列 ${r.data.rollback.enrollments_removed} 人；${t('p5.notRollbackable')}：${r.data.rollback.not_rollbackable.join('、')}`)
    onChange(r.data)
  }
  if (task.mode === 'help') return <Card size="small" title={t('p5.help')}><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{task.answer}</Typography.Paragraph></Card>
  return <Card size="small" title={<Space>{task.input_text}<Tag color={SC[task.status]}>{t(`p5.ast_${task.status}`)}</Tag></Space>}>
    {task.status === 'draft' && <>
      <Alert type="info" message={t('p5.questions')} description={<ul>{(task.questions || []).map((q: string) => <li key={q}>{q}</li>)}</ul>} />
      <Space.Compact style={{ width: '100%', marginTop: 8 }}>
        <Input value={ans} onChange={e => setAns(e.target.value)} placeholder={t('p5.answer')} />
        <Button type="primary" onClick={() => act('answer', { text: ans })}>{t('p5.answer')}</Button>
      </Space.Compact></>}
    {(task.plan || []).length > 0 && <>
      <Descriptions size="small" column={4} style={{ marginBottom: 8 }}>
        <Descriptions.Item label={t('p5.effective')}>{task.effective_autonomy}{task.limited_by && <Tag style={{ marginLeft: 4 }}>{t('p5.limitedBy')}</Tag>}</Descriptions.Item>
        <Descriptions.Item label={t('p5.estimate')}>{task.estimate?.low} / {task.estimate?.mid} / {task.estimate?.high}</Descriptions.Item>
        <Descriptions.Item label={t('p5.minutes')}>{task.estimate?.minutes}</Descriptions.Item>
        <Descriptions.Item label={`${t('p5.used')} / ${t('p5.held')}`}>{task.credits_used} / {task.credits_held}</Descriptions.Item>
      </Descriptions>
      <Table size="small" rowKey="no" pagination={false} dataSource={task.plan} rowClassName={(r: any) => r.no - 1 === task.current_step ? 'ant-table-row-selected' : ''} columns={[
        { title: t('p5.step'), dataIndex: 'no', width: 50 }, { title: t('p5.agentName'), dataIndex: 'agent', width: 70 },
        { title: t('p5.tool'), dataIndex: 'zh' }, { title: t('p5.qty'), dataIndex: 'qty', width: 80 },
        { title: t('p5.credits'), dataIndex: 'credits_high', width: 110 },
        { title: '', width: 160, render: (_: any, r: any) => <Space>{r.approval && <Tag color="orange">{t('p5.needApproval')}</Tag>}{r.forbidden && <Tag color="red">{t('p5.forbidden')}</Tag>}
          {r.approved && <Tag color="green">已通过</Tag>}{r.rejected && <Tag>已拒绝</Tag>}</Space> },
      ]} />
    </>}
    {task.status === 'waiting_approval' && <Alert style={{ marginTop: 8 }} type="warning" message={`${t('p5.waiting')}：${task.pending_approval?.zh}（${task.pending_approval?.qty}）`}
      action={can('agent.approve') && <Space><Button type="primary" onClick={() => act('decide', { approve: true })}>{t('p5.approve')}</Button>
        <Button danger onClick={() => act('decide', { approve: false })}>{t('p5.reject')}</Button></Space>} />}
    {task.status === 'paused' && task.pause_reason === 'over_estimate' && <Alert style={{ marginTop: 8 }} type="warning" message="实际消耗已超过预估中位数的 120%，任务已自动暂停，确认后可继续。" />}
    <Space style={{ marginTop: 8 }} wrap>
      {task.status === 'plan_pending' && <Button type="primary" onClick={() => act('confirm')}>{t('p5.confirm')}</Button>}
      {['running', 'waiting_approval', 'queued'].includes(task.status) && <Button onClick={() => act('pause')}>{t('p5.pause')}</Button>}
      {task.status === 'paused' && <Button type="primary" onClick={() => act('resume')}>{t('p5.resume')}</Button>}
      {!['completed', 'partial', 'cancelled', 'failed'].includes(task.status) && <Button danger onClick={() => act('cancel')}>{t('p5.cancel')}</Button>}
      {!['running', 'queued', 'draft', 'plan_pending'].includes(task.status) && task.context?.enrollment_ids !== undefined && <Button onClick={() => act('rollback')}>{t('p5.rollback')}</Button>}
    </Space>
    {(task.logs || []).length > 0 && <Card size="small" title={t('p5.log')} style={{ marginTop: 8 }}>
      <Timeline items={task.logs.map((l: any) => ({ color: l.status === 'failed' ? 'red' : l.status === 'ok' ? 'green' : 'gray',
        children: <div><b>{l.agent} · {l.tool}</b> <Tag>{l.status}</Tag> {l.credits ? `${l.credits} 点` : ''} {l.duration_ms} ms
          <div style={{ color: '#888', fontSize: 12 }}>{JSON.stringify(l.output)}</div></div> }))} />
    </Card>}
  </Card>
}

function Ledger() {
  const { t } = useTranslation()
  const [d, setD] = useState<any>()
  useEffect(() => { api.get('/credits').then(r => setD(r.data)) }, [])
  return <>{d && <><Statistic title={t('p5.balance')} value={d.balance} />
    <Table rowKey="id" size="small" dataSource={d.items} columns={[
      { title: '时间', dataIndex: 'at', render: v => new Date(v).toLocaleString() }, { title: '类型', dataIndex: 'entry_type' },
      { title: '动作', dataIndex: 'action' }, { title: '数量', dataIndex: 'quantity' }, { title: '单价', dataIndex: 'unit_price' },
      { title: '余额影响', dataIndex: 'credits' }, { title: '结果', dataIndex: 'result' }, { title: '备注', dataIndex: 'note' }]} /></>}</>
}

export default function AgentPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [text, setText] = useState('')
  const [autonomy, setAutonomy] = useState('L1')
  const [budget, setBudget] = useState<number | null>(null)
  const [cur, setCur] = useState<any>()
  const [list, setList] = useState<any>({ items: [] })
  const [view, setView] = useState<any>()
  const load = useCallback(async () => setList((await api.get('/agent/tasks', { params: { page_size: 50 } })).data), [])
  useEffect(() => { load() }, [load])
  const submit = async () => { const r = await api.post('/agent/tasks', { text, autonomy, budget }); setCur((await api.get(`/agent/tasks/${r.data.id}`)).data); load() }
  const change = (x: any) => { setCur(x); load() }
  return <Tabs items={[{ key: 't', label: t('menu.agent'), children: <Row gutter={16}>
    <Col span={15}>
      <Card size="small">
        <Input.TextArea rows={3} value={text} onChange={e => setText(e.target.value)} placeholder={t('p5.agentPlaceholder')} maxLength={1000} showCount />
        <Space style={{ marginTop: 8 }}>
          <Select value={autonomy} onChange={setAutonomy} style={{ width: 160 }} options={[{ value: 'L0', label: 'L0 仅建议' }, { value: 'L1', label: 'L1 辅助执行' }, { value: 'L2', label: 'L2 批量审批' }]} />
          <InputNumber placeholder={t('p5.budget')} value={budget} onChange={v => setBudget(v)} min={0} style={{ width: 160 }} />
          <Button type="primary" disabled={text.trim().length < 5} onClick={submit}>{t('p5.send')}</Button>
        </Space>
      </Card>
      {cur && <div style={{ marginTop: 12 }}><TaskCard task={cur} onChange={change} /></div>}
    </Col>
    <Col span={9}>
      <Card size="small" title={<Space>{t('p5.tasks')}<Tag>{t('p5.balance')} {list.balance}</Tag></Space>}>
        <Table rowKey="id" size="small" dataSource={list.items} pagination={{ pageSize: 10 }}
          onRow={(r: any) => ({ onClick: async () => setView((await api.get(`/agent/tasks/${r.id}`)).data), style: { cursor: 'pointer' } })} columns={[
            { title: '任务', dataIndex: 'input_text', ellipsis: true }, { title: t('p5.status'), dataIndex: 'status', render: s => <Tag color={SC[s]}>{t(`p5.ast_${s}`)}</Tag> }]} />
      </Card>
    </Col>
    <Drawer width={820} open={!!view} onClose={() => setView(undefined)}>{view && <TaskCard task={view} onChange={x => { setView(x); load() }} />}</Drawer>
  </Row> }, ...(can('billing.read') ? [{ key: 'l', label: t('p5.ledger'), children: <Ledger /> }] : [])]} />
}
