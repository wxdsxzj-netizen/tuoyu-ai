/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Drawer, Input, InputNumber, Row, Select, Space, Table, Tabs, Tag, Timeline, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { useServerPage, PageError } from '../useServerPage'
import { useSubmission } from '../api/useSubmission'
import { useAuth } from '../auth'

const SC: Record<string, string> = { completed: 'green', partial: 'gold', failed: 'red', waiting_approval: 'orange', paused: 'purple', running: 'blue', cancelled: 'default' }

function TaskCard({ task, onChange }: { task: any; onChange: (t: any) => void }) {
  const { t } = useTranslation()
  const { can, me } = useAuth()
  const [ans, setAns] = useState('')
  const [evidence, setEvidence] = useState('')
  const [pollError, setPollError] = useState(false)
  const changes = useRef(onChange)
  changes.current = onChange
  const generation = useRef(0)
  const busy = useRef(false)
  const [acting, setActing] = useState(false)
  useEffect(() => {
    if (!['queued', 'running'].includes(task.status) && !task.control_requested) return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    const poll = async () => {
      const revision = generation.current
      try {
        if (!busy.current) {
          const response = await api.get(`/agent/tasks/${task.id}`)
          if (active && revision === generation.current) { setPollError(false); failures = 0; changes.current(response.data) }
        }
      } catch { if (active) { setPollError(true); failures++ } }
      if (active) timer = setTimeout(poll, Math.min(10000, 2000 * (failures + 1)))
    }
    timer = setTimeout(poll, 2000)
    return () => { active = false; clearTimeout(timer) }
  }, [task.id, task.status, task.control_requested])
  const act = async (a: string, body?: any) => {
    if (busy.current) return
    busy.current = true; generation.current++; setActing(true)
    try {
      const r = await api.post(`/agent/tasks/${task.id}:${a}`, body)
      if (r.data.rollback) message.info(`${t('p5.rollbackDone')}：移出序列 ${r.data.rollback.enrollments_removed} 人；${t('p5.notRollbackable')}：${r.data.rollback.not_rollbackable.join('、')}`)
      onChange(r.data)
    } catch { /* The API interceptor reports the failure. */ }
    finally { generation.current++; busy.current = false; setActing(false) }
  }
  if (task.mode === 'help') return <Card size="small" title={t('p5.help')}><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{task.answer}</Typography.Paragraph></Card>
  return <Card size="small" title={<Space>{task.input_text}<Tag color={SC[task.status]}>{t(`p5.ast_${task.status}`)}</Tag></Space>}><fieldset disabled={acting} style={{ border: 0, padding: 0, margin: 0 }}>
    {pollError && <Alert type="warning" message="进度刷新失败，正在重试；请勿重复创建任务。" />}
    {task.control_requested && <Alert type="info" message={task.control_requested === 'cancel' ? '已请求取消，当前步骤结束后停止。' : '已请求暂停，当前步骤结束后停止。'} />}
    {task.status === 'paused' && ['outcome_unknown', 'legacy_running_unknown'].includes(task.pause_reason) && <Alert type="warning" message="执行结果尚未确认，请核对供应商记录；不能直接继续或重复执行。"
      description={task.pause_reason === 'outcome_unknown' && me?.user.role_code === 'owner' && can('agent.approve') && <Space direction="vertical" style={{ width: '100%' }}>
        <Input.TextArea aria-label="供应商未执行证据" value={evidence} onChange={event => setEvidence(event.target.value)} maxLength={500} placeholder="仅在供应商确认本步未执行后，填写回执等核对证据。" />
        <Button disabled={evidence.trim().length < 10} onClick={() => act('reconcile-not-applied', { evidence })}>确认未执行并重试</Button>
      </Space>} />}
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
      {task.status === 'paused' && !['outcome_unknown', 'legacy_running_unknown'].includes(task.pause_reason) && <Button type="primary" onClick={() => act('resume')}>{t('p5.resume')}</Button>}
      {!['completed', 'partial', 'cancelled', 'failed'].includes(task.status) && <Button danger onClick={() => act('cancel')}>{t('p5.cancel')}</Button>}
      {!['running', 'queued', 'draft', 'plan_pending'].includes(task.status) && task.context?.enrollment_ids !== undefined && <Button onClick={() => act('rollback')}>{t('p5.rollback')}</Button>}
    </Space>
    {(task.logs || []).length > 0 && <Card size="small" title={t('p5.log')} style={{ marginTop: 8 }}>
      <Timeline items={task.logs.map((l: any) => ({ color: l.status === 'failed' ? 'red' : l.status === 'ok' ? 'green' : 'gray',
        children: <div><b>{l.agent} · {l.tool}</b> <Tag>{l.status}</Tag> {l.credits ? `${l.credits} 点` : ''} {l.duration_ms} ms
          <div style={{ color: '#888', fontSize: 12 }}>{JSON.stringify(l.output)}</div></div> }))} />
    </Card>}
  </fieldset></Card>
}

export default function AgentPage() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const location = useLocation()
  const prefill = typeof location.state?.prefill === 'string' ? location.state.prefill.slice(0, 1000) : ''
  const submission = useSubmission<{ text: string; autonomy: string; budget: number | null }>(`agent:${me?.tenant.id}:${me?.user.id}`)
  const [text, setText] = useState(submission.pending?.payload.text || prefill)
  const [autonomy, setAutonomy] = useState(submission.pending?.payload.autonomy || 'L1')
  const [budget, setBudget] = useState<number | null>(submission.pending?.payload.budget ?? null)
  const [cur, setCur] = useState<any>()
  const page = useServerPage('/agent/tasks', {}, 'page_size')
  const { data: list, reload: load } = page
  const [view, setView] = useState<any>()
  const submit = async () => {
    const body = { text, autonomy, budget }
    const r = await submission.run(body, request_id => api.post('/agent/tasks', { ...body, request_id }))
    if (r) { setCur(r.data); setText(''); load() }
  }
  const change = (x: any) => { setCur(x); load() }
  return <Tabs items={[{ key: 't', label: t('menu.agent'), children: <Row gutter={16}>
    <Col span={15}>
      <Card size="small">
        {submission.error && <Alert type="error" showIcon message={submission.error} style={{ marginBottom: 8 }} />}
        {submission.pending && !submission.loading && <Alert type="warning" showIcon message="上次任务提交结果待确认，请重试原请求。" />}
        <Input.TextArea rows={3} disabled={!!submission.pending} value={text} onChange={e => setText(e.target.value)} placeholder={t('p5.agentPlaceholder')} maxLength={1000} showCount />
        <Space style={{ marginTop: 8 }}>
          <Select disabled={!!submission.pending} value={autonomy} onChange={setAutonomy} style={{ width: 160 }} options={[{ value: 'L0', label: 'L0 仅建议' }, { value: 'L1', label: 'L1 辅助执行' }, { value: 'L2', label: 'L2 批量审批' }]} />
          <InputNumber disabled={!!submission.pending} placeholder={t('p5.budget')} value={budget} onChange={v => setBudget(v)} min={0} style={{ width: 160 }} />
          <Button type="primary" loading={submission.loading} disabled={text.trim().length < 5} onClick={submit}>{t('p5.send')}</Button>
        </Space>
      </Card>
      {cur && <div style={{ marginTop: 12 }}><TaskCard key={cur.id} task={cur} onChange={change} /></div>}
    </Col>
    <Col span={9}>
      <Card size="small" title={<Space>{t('p5.tasks')}<Tag>{t('p5.balance')} {list.balance}</Tag></Space>}>
        <PageError page={page} />
        <Table rowKey="id" size="small" dataSource={list.items} loading={page.loading} pagination={page.pagination}
          onRow={(r: any) => ({ onClick: async () => setView((await api.get(`/agent/tasks/${r.id}`)).data), style: { cursor: 'pointer' } })} columns={[
            { title: '任务', dataIndex: 'input_text', ellipsis: true }, { title: t('p5.status'), dataIndex: 'status', render: s => <Tag color={SC[s]}>{t(`p5.ast_${s}`)}</Tag> }]} />
      </Card>
    </Col>
    <Drawer width={820} open={!!view} onClose={() => setView(undefined)}>{view && <TaskCard key={view.id} task={view} onChange={x => { setView(x); load() }} />}</Drawer>
  </Row> }]} />
}
