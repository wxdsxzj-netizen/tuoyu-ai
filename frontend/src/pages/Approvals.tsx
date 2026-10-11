/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Collapse, Empty, Input, List, Modal, Select, Space, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

function DraftCard({ d, onDone }: { d: any; onDone: () => void }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [subject, setSubject] = useState(d.subject)
  const [body, setBody] = useState(d.body)
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  useEffect(() => { setSubject(d.subject); setBody(d.body) }, [d.id, d.subject, d.body])
  const edited = subject !== d.subject || body !== d.body
  const perform = async (action: () => Promise<unknown>) => {
    if (pending.current) return
    pending.current = true; setBusy(true)
    try { await action(); await onDone() } catch { /* The API client reports failure; retain the editor. */ }
    finally { pending.current = false; setBusy(false) }
  }
  const decide = async (decision: string) => {
    await perform(async () => { await api.post(`/approvals/${d.id}:decide`, { decision, ...(edited ? { subject, body } : {}) }); message.success(t('p3.done')) })
  }
  const qa = d.qa_detail || {}
  return (
    <Card size="small" title={<Space>{d.contact_name} &lt;{d.contact_email}&gt;<Tag>{d.sequence_name}</Tag><Tag>{t('p3.step')} {d.step_no}</Tag></Space>}
      extra={<Space><Tag color={d.qa_score >= 85 ? 'green' : d.qa_score >= 80 ? 'gold' : 'red'}>{t('p3.qaScore')} {d.qa_score}</Tag>
        {d.personalization === 'degraded' && <Tag color="orange">{t('p3.degraded')}</Tag>}<Tag>{d.model}</Tag></Space>}>
      {(qa.vetoes || []).map((v: any) => <Alert key={v.code} type="error" showIcon message={`${t(`p3.qa_${v.code}`, { defaultValue: String(v.code) })}：${(v.items || []).join('，')}`} style={{ marginBottom: 6 }} />)}
      <Input disabled={busy} value={subject} onChange={(e) => setSubject(e.target.value)} style={{ marginBottom: 8 }} />
      <Input.TextArea disabled={busy} value={body} onChange={(e) => setBody(e.target.value)} autoSize={{ minRows: 6 }} />
      <Collapse ghost items={[{ key: 'q', label: t('p3.qaDetail'), children: <Space wrap>
        {(qa.issues || []).map((i: any, n: number) => <Tag key={n}>{t(`p3.qa_${i.code}`, { defaultValue: String(i.code) })}{i.items ? `：${i.items.join(',')}` : ''}</Tag>)}
        <Tag>{t('p3.words')} {qa.words}</Tag>{(d.facts_used || []).map((f: string) => <Tag color="blue" key={f}>{f}</Tag>)}</Space> }]} />
      <Typography.Text type="secondary">{t('p3.footerNote')}</Typography.Text>
      {can('sequence.approve') && <Space style={{ marginTop: 8, display: 'flex' }}>
        <Button disabled={busy} type="primary" onClick={() => decide('approve')}>{edited ? t('p3.saveApprove') : t('p3.approve')}</Button>
        <Button disabled={busy} danger onClick={() => decide('reject')}>{t('p3.rejectDraft')}</Button>
        <Button disabled={busy} onClick={() => {
          const regenerate = () => perform(() => api.post(`/approvals/${d.id}:regenerate`))
          if (edited) Modal.confirm({ title: '重新生成草稿？', content: '当前未保存修改会被新草稿替换。', onOk: regenerate })
          else regenerate()
        }}>{t('p3.regenerate')}</Button>
      </Space>}
    </Card>
  )
}

export default function ApprovalsPage() {
  const { t } = useTranslation()
  const [st, setSt] = useState('pending_approval')
  const [data, setData] = useState<any>()
  const load = useCallback(async () => setData((await api.get('/approvals', { params: { status: st, size: 50 } })).data), [st])
  useEffect(() => { load() }, [load])
  if (!data) return null
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Select value={st} onChange={setSt} style={{ width: 160 }} options={['pending_approval', 'needs_edit', 'approved', 'rejected', 'sent', 'blocked'].map((x) => ({ value: x, label: t(`p3.dr_${x}`) }))} />
        {data.enrollments_pending > 0 && <Tag color="gold">{t('p3.enrollPending', { n: data.enrollments_pending })}</Tag>}
        {data.sequences_pending.length > 0 && <Tag color="gold">{t('p3.seqPending', { n: data.sequences_pending.length })}</Tag>}
        {data.total > 0 && st === 'pending_approval' && <Button onClick={async () => {
          for (const d of data.items.filter((x: any) => x.qa_score >= 80)) await api.post(`/approvals/${d.id}:decide`, { decision: 'approve' })
          load()
        }}>{t('p3.approveAll')}</Button>}
      </Space>
      {data.items.length ? <List dataSource={data.items} renderItem={(d: any) => <List.Item><DraftCard key={d.id} d={d} onDone={load} /></List.Item>} /> : <Empty />}
    </Space>
  )
}
