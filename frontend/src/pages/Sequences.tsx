/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Drawer, Form, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, Timeline, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const SEQ_COLOR: Record<string, string> = { draft: 'default', pending_approval: 'gold', active: 'green', paused: 'orange', completed: 'blue', archived: 'default' }

function StepEditor({ seq, onSaved }: { seq: any; onSaved: () => void }) {
  const { t } = useTranslation()
  const [edit, setEdit] = useState<any>()
  return (
    <>
      <Timeline items={seq.steps.map((s: any) => ({ children: (
        <Card size="small" title={`${t('p3.step')} ${s.step_no} · ${t(`p3.pp_${s.purpose}`)} · ${s.step_no === 1 ? t('p3.day0') : t('p3.waitN', { n: s.wait_days })}`}
          extra={<Space><Tag>{s.mode === 'ai' ? t('p3.aiWrite') : t('p3.template')}</Tag><a onClick={() => setEdit(s)}>{t('p3.edit')}</a></Space>}>
          {s.mode === 'template' ? <><b>{s.subject_tpl}</b><div style={{ whiteSpace: 'pre-wrap' }}>{s.body_tpl}</div></> : <Typography.Text type="secondary">{s.instructions || t('p3.aiDefault')}</Typography.Text>}
        </Card>) }))} />
      <Button onClick={() => setEdit({ step_no: (seq.steps.at(-1)?.step_no || 0) + 1, wait_days: 3, mode: 'ai', purpose: 'followup' })}>{t('p3.addStep')}</Button>
      <Modal open={!!edit} title={`${t('p3.step')} ${edit?.step_no}`} footer={null} width={680} onCancel={() => setEdit(undefined)} destroyOnClose>
        <Alert style={{ marginBottom: 12 }} type="info" showIcon message={t('p3.tplVars')} />
        <Form layout="vertical" initialValues={edit} onFinish={async (v) => { await api.put(`/sequences/${seq.id}/steps/${edit.step_no}`, { ...edit, ...v }); setEdit(undefined); onSaved() }}>
          <Space>
            <Form.Item name="purpose" label={t('p3.purpose')}><Select style={{ width: 140 }} options={['first', 'followup', 'breakup'].map((x) => ({ value: x, label: t(`p3.pp_${x}`) }))} /></Form.Item>
            <Form.Item name="wait_days" label={t('p3.waitDays')}><InputNumber min={0} max={60} /></Form.Item>
            <Form.Item name="mode" label={t('p3.mode')}><Select style={{ width: 140 }} options={[{ value: 'ai', label: t('p3.aiWrite') }, { value: 'template', label: t('p3.template') }]} /></Form.Item>
          </Space>
          <Form.Item noStyle shouldUpdate>{({ getFieldValue }) => getFieldValue('mode') === 'template' ? <>
            <Form.Item name="subject_tpl" label={t('p3.subjectTpl')} rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="body_tpl" label={t('p3.bodyTpl')} rules={[{ required: true }]}><Input.TextArea rows={8} /></Form.Item></>
            : <Form.Item name="instructions" label={t('p3.instructions')}><Input.TextArea rows={4} /></Form.Item>}</Form.Item>
          <Button type="primary" htmlType="submit">{t('p3.save')}</Button>
        </Form>
      </Modal>
    </>
  )
}

function Enrollments({ seq }: { seq: any }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [opts, setOpts] = useState<any[]>([])
  const [pick, setPick] = useState<string[]>([])
  const [events, setEvents] = useState<any>()
  const load = useCallback(async () => setRows((await api.get(`/sequences/${seq.id}/enrollments`, { params: { size: 200 } })).data.items), [seq.id])
  useEffect(() => { load() }, [load])
  const search = async (q: string) => setOpts((await api.get('/contacts', { params: { q, size: 20 } })).data.items.map((c: any) => ({ value: c.id, label: `${c.full_name} <${c.email || '—'}> · ${c.company_name || ''}` })))
  const enroll = async () => {
    const r = await api.post(`/sequences/${seq.id}/enrollments`, { contact_ids: pick })
    message.info(t('p3.enrollResult', { ok: r.data.enrolled, bad: r.data.blocked }))
    r.data.results.filter((x: any) => !x.enrolled).forEach((x: any) => message.warning(`${x.blocks.map((b: any) => b.code).join(', ')}`))
    setPick([]); load()
  }
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Select mode="multiple" showSearch filterOption={false} onSearch={search} style={{ width: 460 }} value={pick} onChange={setPick} options={opts} placeholder={t('p3.searchContacts')} />
        <Button type="primary" disabled={!pick.length} onClick={enroll}>{t('p3.enroll')}</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={rows} columns={[
        { title: t('p3.contact'), render: (_: any, r: any) => `${r.contact_name} <${r.contact_email || '—'}>` },
        { title: t('p3.status'), dataIndex: 'status', render: (v: string, r: any) => <Tag>{t(`p3.es_${v}`)}{r.paused_reason ? `·${r.paused_reason}` : ''}{r.ended_reason ? `·${r.ended_reason}` : ''}</Tag> },
        { title: t('p3.step'), dataIndex: 'current_step' },
        { title: t('p3.nextRun'), dataIndex: 'next_run_at', render: (v: string) => v ? new Date(v).toLocaleString() : '—' },
        { title: '', render: (_: any, r: any) => <Space>
          <a onClick={async () => setEvents((await api.get('/delivery/events', { params: { enrollment_id: r.id } })).data.items)}>{t('p3.log')}</a>
          {r.status === 'pending_approval' && <a onClick={async () => { await api.post(`/enrollments/${r.id}:approve`); load() }}>{t('p3.approve')}</a>}
          {['scheduled', 'active'].includes(r.status) && <a onClick={async () => { await api.post(`/enrollments/${r.id}:pause`); load() }}>{t('p3.pause')}</a>}
          {r.status === 'paused' && <a onClick={async () => { await api.post(`/enrollments/${r.id}:resume`); load() }}>{t('p3.resume')}</a>}
          {['pending_approval', 'scheduled', 'active', 'paused'].includes(r.status) && <a onClick={async () => { await api.post(`/enrollments/${r.id}:remove`); load() }}>{t('p3.remove')}</a>}
        </Space> },
      ]} />
      <Modal open={!!events} footer={null} title={t('p3.log')} onCancel={() => setEvents(undefined)} width={640}>
        <Timeline items={(events || []).map((e: any) => ({ color: e.type === 'sent' ? 'green' : ['gate_blocked', 'hard_bounce'].includes(e.type) ? 'red' : 'blue',
          children: `${new Date(e.occurred_at).toLocaleString()} · ${t(`p3.et_${e.type}`, { defaultValue: String(e.type) })}${e.reason_code ? ` · ${e.reason_code}` : ''}` }))} />
      </Modal>
    </>
  )
}

export default function SequencesPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [mbs, setMbs] = useState<any[]>([])
  const [icps, setIcps] = useState<any[]>([])
  const [creating, setCreating] = useState(false)
  const [cur, setCur] = useState<any>()
  const load = useCallback(async () => {
    const r = await api.get('/sequences'); setRows(r.data)
    if (cur) setCur(r.data.find((s: any) => s.id === cur.id))
  }, [cur])
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    api.get('/mailboxes').then((r) => setMbs(r.data)).catch(() => 0)
    api.get('/icps').then((r) => setIcps(r.data.items || r.data)).catch(() => 0)
  }, [])
  const action = async (s: any, a: string) => { await api.post(`/sequences/${s.id}:${a}`); message.success(t('p3.done')); load() }
  return (
    <>
      {can('sequence.manage') && <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setCreating(true)}>{t('p3.newSeq')}</Button>}
      <Table rowKey="id" dataSource={rows} columns={[
        { title: t('p3.seqName'), dataIndex: 'name', render: (v: string, r: any) => <a onClick={() => setCur(r)}>{v}</a> },
        { title: t('p3.status'), dataIndex: 'status', render: (v: string, r: any) => <Tag color={SEQ_COLOR[v]}>{t(`p3.ss_${v}`)}{r.paused_reason ? `·${r.paused_reason}` : ''}</Tag> },
        { title: t('p3.autonomy'), dataIndex: 'autonomy', render: (v: string) => t(`p3.au_${v}`) },
        { title: t('p3.steps'), render: (_: any, r: any) => r.steps.length },
        { title: t('p3.funnel'), render: (_: any, r: any) => Object.entries(r.stats).map(([k, v]) => <Tag key={k}>{t(`p3.es_${k}`)} {String(v)}</Tag>) },
        { title: t('p3.sent'), dataIndex: 'sent_count' },
        { title: '', render: (_: any, r: any) => <Space>
          {can('sequence.manage') && ['draft', 'paused'].includes(r.status) && <a onClick={() => action(r, 'submit')}>{t('p3.submitApproval')}</a>}
          {can('sequence.approve') && r.status === 'pending_approval' && <a onClick={() => action(r, 'approve')}>{t('p3.approve')}</a>}
          {can('sequence.manage') && r.status === 'active' && <a onClick={() => action(r, 'pause')}>{t('p3.pause')}</a>}
          {can('sequence.manage') && r.status === 'paused' && <a onClick={() => action(r, 'resume')}>{t('p3.resume')}</a>}
          {can('sequence.manage') && <a onClick={() => action(r, 'copy')}>{t('p3.copy')}</a>}
          {can('sequence.manage') && !['active', 'archived'].includes(r.status) && <a onClick={() => action(r, 'archive')}>{t('p3.archive')}</a>}
        </Space> },
      ]} />
      <Modal open={creating} title={t('p3.newSeq')} footer={null} onCancel={() => setCreating(false)} destroyOnClose>
        <Alert style={{ marginBottom: 12 }} type="info" showIcon message={t('p3.defaultSteps')} />
        <Form layout="vertical" initialValues={{ autonomy: 'L1', language: 'en', window_start: 8, window_end: 17 }}
          onFinish={async (v) => { await api.post('/sequences', v); setCreating(false); load() }}>
          <Form.Item name="name" label={t('p3.seqName')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="mailbox_ids" label={t('p3.mailboxes')} rules={[{ required: true }]}>
            <Select mode="multiple" options={mbs.map((m) => ({ value: m.id, label: `${m.address}（${t(`p3.ms_${m.status}`)}）` }))} /></Form.Item>
          <Form.Item name="icp_id" label="ICP"><Select allowClear options={icps.map((i: any) => ({ value: i.id, label: i.name }))} /></Form.Item>
          <Form.Item name="autonomy" label={t('p3.autonomy')}><Select options={['L0', 'L1', 'L2'].map((x) => ({ value: x, label: t(`p3.au_${x}`) }))} /></Form.Item>
          <Space>
            <Form.Item name="language" label={t('p3.language')}><Select style={{ width: 120 }} options={['en', 'es', 'de', 'fr', 'pt', 'ru', 'ar'].map((x) => ({ value: x, label: x }))} /></Form.Item>
            <Form.Item name="window_start" label={t('p3.windowStart')}><InputNumber min={0} max={23} /></Form.Item>
            <Form.Item name="window_end" label={t('p3.windowEnd')}><InputNumber min={1} max={24} /></Form.Item>
            <Form.Item name="send_limit" label={t('p3.sendLimit')}><InputNumber min={0} /></Form.Item>
          </Space>
          <Button type="primary" htmlType="submit">{t('p3.save')}</Button>
        </Form>
      </Modal>
      <Drawer open={!!cur} width={860} title={cur?.name} onClose={() => setCur(undefined)} destroyOnClose>
        {cur && <Tabs items={[
          { key: 's', label: t('p3.steps'), children: <StepEditor seq={cur} onSaved={load} /> },
          { key: 'e', label: t('p3.enrollments'), children: <Enrollments seq={cur} /> },
        ]} />}
      </Drawer>
    </>
  )
}
