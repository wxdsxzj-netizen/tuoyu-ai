/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Col, Descriptions, Drawer, Input, Modal, Row, Select, Space, Statistic, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useServerPage, PageError } from '../useServerPage'

const REASONS = ['not_genuine', 'unclear', 'not_icp', 'duplicate', 'existing_customer', 'competitor', 'other']
const NEEDS = ['product', 'quantity', 'use', 'timing']

export default function HandoffsPage() {
  const { t } = useTranslation()
  const [status, setStatus] = useState('pending')
  const page = useServerPage('/handoffs', { status })
  const { rows, reload } = page
  const [stats, setStats] = useState<any>()
  const [view, setView] = useState<any>()
  const [rej, setRej] = useState<{ reason?: string; note?: string } | null>(null)
  const load = useCallback(async () => { reload(); setStats((await api.get('/handoffs/stats')).data) }, [reload])
  useEffect(() => { api.get('/handoffs/stats').then(r => setStats(r.data)) }, [])
  const open = async (id: string) => setView((await api.get(`/handoffs/${id}`)).data)
  const accept = async () => {
    const r = await api.post(`/handoffs/${view.handoff.id}:accept`)
    message.success(r.data.is_qualified ? t('p4.acceptedQualified') : t('p4.accepted'))
    setView(undefined); load()
  }
  const reject = async () => {
    await api.post(`/handoffs/${view.handoff.id}:reject`, rej)
    setRej(null); setView(undefined); load()
  }
  const h = view?.handoff
  return (
    <>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}><Card size="small"><Statistic title={t('p4.hs_pending')} value={stats?.pending ?? 0} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('p4.hs_accepted')} value={stats?.accepted ?? 0} valueStyle={{ color: '#3f8600' }} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('p4.hs_rejected')} value={stats?.rejected ?? 0} valueStyle={{ color: '#cf1322' }} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('p4.acceptRate')} value={stats?.acceptance_rate == null ? '—' : `${Math.round(stats.acceptance_rate * 100)}%`} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('p4.qualifiedOpps')} value={stats?.qualified_opportunities ?? 0} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('p4.hs_expired')} value={stats?.expired ?? 0} /></Card></Col>
      </Row>
      <Tabs activeKey={status} onChange={setStatus} items={['pending', 'accepted', 'rejected', 'expired'].map((s) => ({ key: s, label: t(`p4.hs_${s}`) }))} />
      <PageError page={page} />
      <Table loading={page.loading} pagination={page.pagination} rowKey="id" size="small" dataSource={rows} columns={[
        { title: t('p4.summary'), dataIndex: 'summary', render: (v: string, r: any) => <a onClick={() => open(r.id)}>{v || '—'}</a> },
        { title: t('p3.category'), dataIndex: 'category', render: (v: string) => <Tag color="green">{t(`p3.rc_${v}`, { defaultValue: v })}</Tag> },
        { title: t('p4.needs'), dataIndex: 'needs', render: (n: any) => <Space size={2} wrap>{NEEDS.map((k) => <Tag key={k} color={n?.[k] ? 'blue' : 'default'}>{t(`p4.need_${k}`)}</Tag>)}</Space> },
        { title: t('p4.assignee'), dataIndex: 'assignee_id', render: (v: string) => v ? '✓' : <Tag>{t('p4.pool')}</Tag> },
        { title: t('p4.slaDue'), dataIndex: 'sla_due_at', render: (v: string) => v ? <span style={{ color: new Date(v) < new Date() ? 'red' : undefined }}>{new Date(v).toLocaleString()}</span> : '—' },
        { title: t('p4.reassigned'), dataIndex: 'reassign_count' },
        { title: t('p4.createdAt'), dataIndex: 'created_at', render: (v: string) => new Date(v).toLocaleString() },
      ]} />
      <Drawer open={!!view} width={760} title={t('p4.handoff')} onClose={() => setView(undefined)}
        extra={h?.status === 'pending' && <Space><Button danger onClick={() => setRej({})}>{t('p4.reject')}</Button><Button type="primary" onClick={accept}>{t('p4.accept')}</Button></Space>}>
        {h && <>
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label={t('p4.summary')}>{h.summary}</Descriptions.Item>
            {NEEDS.map((k) => <Descriptions.Item key={k} label={t(`p4.need_${k}`)}>{h.needs?.[k] || <Tag>{t('p4.missing')}</Tag>}</Descriptions.Item>)}
            <Descriptions.Item label={t('p4.research')}><pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(h.research, null, 2)}</pre></Descriptions.Item>
            {h.reject_reason && <Descriptions.Item label={t('p4.rejectReason')}>{t(`p4.rr_${h.reject_reason}`)} {h.reject_note}</Descriptions.Item>}
          </Descriptions>
          <Card size="small" title={t('p4.suggestedReply')} style={{ marginTop: 12 }}><pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{h.suggested_reply}</pre></Card>
          <Card size="small" title={t('p4.thread')} style={{ marginTop: 12 }}>
            {view.messages.map((m: any) => <div key={m.id} style={{ marginBottom: 10 }}><Tag>{t(`p4.dir_${m.direction}`)}</Tag><b>{m.subject}</b>
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{m.body}</pre>
              {m.body_translated && <pre style={{ whiteSpace: 'pre-wrap', margin: 0, color: '#888', fontFamily: 'inherit' }}>{m.body_translated}</pre>}</div>)}
          </Card>
        </>}
      </Drawer>
      <Modal open={!!rej} title={t('p4.reject')} onCancel={() => setRej(null)} onOk={reject} okButtonProps={{ disabled: !rej?.reason || (rej.reason === 'other' && !rej.note?.trim()) }}>
        <Select style={{ width: '100%', marginBottom: 8 }} placeholder={t('p4.rejectReason')} value={rej?.reason} onChange={(v) => setRej({ ...rej, reason: v })}
          options={REASONS.map((r) => ({ value: r, label: t(`p4.rr_${r}`) }))} />
        <Input.TextArea rows={3} placeholder={t('p4.rejectNote')} value={rej?.note} onChange={(e) => setRej({ ...rej, note: e.target.value })} />
      </Modal>
    </>
  )
}
