/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Badge, Button, Card, Col, Empty, Input, List, Radio, Row, Select, Space, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const CAT_COLOR: Record<string, string> = { inquiry: 'green', positive: 'green', sample: 'cyan', meeting: 'blue', referral: 'purple', not_interested: 'default', unsubscribe: 'red', auto_reply: 'gold' }

export default function InboxPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [status, setStatus] = useState<string>()
  const [rows, setRows] = useState<any[]>([])
  const [cur, setCur] = useState<string>()
  const [detail, setDetail] = useState<any>()
  const [draft, setDraft] = useState('')
  const [sug, setSug] = useState<any>()
  const [check, setCheck] = useState<any>()
  const [bilingual, setBilingual] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    const r = await api.get('/conversations', { params: { status, size: 100 } })
    setRows(r.data.items)
  }, [status])
  const open = useCallback(async (id: string) => {
    setCur(id); setSug(undefined); setCheck(undefined); setDraft(''); setBilingual(null)
    const r = await api.get(`/conversations/${id}`)
    setDetail(r.data)
  }, [])
  useEffect(() => { load() }, [load])

  const suggest = async (tone = 'professional') => {
    setBusy(true)
    try { const r = await api.post(`/conversations/${cur}:suggest`, null, { params: { tone } }); setSug(r.data); if (r.data.drafts[0]) setDraft(r.data.drafts[0].body) } finally { setBusy(false) }
  }
  const precheck = async () => {
    const r = await api.post(`/conversations/${cur}:check`, { body: draft })
    setCheck(r.data)
    if (/[\u4e00-\u9fff]/.test(draft)) {
      const tr = await api.post('/inbox/translate', { text: draft, target: detail?.conversation.language || 'en' })
      setBilingual(tr.data.available ? tr.data.text : null)
    }
  }
  const send = async () => {
    setBusy(true)
    try {
      await api.post(`/conversations/${cur}:reply`, { body: draft })
      message.success(t('p4.sent')); setDraft(''); setCheck(undefined); open(cur!); load()
    } finally { setBusy(false) }
  }
  const addNote = async () => { await api.post(`/conversations/${cur}/notes`, { body: note }); setNote(''); open(cur!) }
  const setConvStatus = async (s: string) => { await api.patch(`/conversations/${cur}`, { status: s }); open(cur!); load() }

  const c = detail?.conversation
  return (
    <Row gutter={16}>
      <Col span={8}>
        <Card size="small" title={t('p4.inbox')} extra={<Radio.Group size="small" value={status ?? ''} onChange={(e) => setStatus(e.target.value || undefined)}
          options={[{ value: '', label: t('p4.all') }, { value: 'unread', label: t('p4.cs_unread') }, { value: 'pending', label: t('p4.cs_pending') }, { value: 'closed', label: t('p4.cs_closed') }]} optionType="button" />}>
          <List size="small" dataSource={rows} locale={{ emptyText: <Empty description={t('p4.noConv')} /> }} renderItem={(r: any) => (
            <List.Item onClick={() => open(r.id)} style={{ cursor: 'pointer', background: r.id === cur ? '#e6f4ff' : undefined, paddingLeft: 8 }}>
              <List.Item.Meta
                title={<Space>{r.status === 'unread' && <Badge status="processing" />}<span>{r.peer_email}</span></Space>}
                description={<Space wrap size={4}><Typography.Text ellipsis style={{ maxWidth: 180 }}>{r.subject}</Typography.Text>
                  {r.last_category && <Tag color={CAT_COLOR[r.last_category]}>{t(`p3.rc_${r.last_category}`, { defaultValue: r.last_category })}</Tag>}
                  {r.needs_human && <Tag color="orange">{t('p4.needsHuman')}</Tag>}</Space>} />
            </List.Item>)} />
        </Card>
      </Col>
      <Col span={16}>
        {!c ? <Empty description={t('p4.pickConv')} /> : (
          <Card size="small" title={<Space>{c.peer_email}<Tag>{t(`p4.cs_${c.status}`)}</Tag>{detail.account && <Tag color="blue">{detail.account.name}</Tag>}
            {detail.handoff && <Tag color="green">{t('p4.handoff')}·{t(`p4.hs_${detail.handoff.status}`)}</Tag>}</Space>}
            extra={<Space><Button size="small" onClick={async () => { await api.post(`/conversations/${cur}:translate`); open(cur!) }}>{t('p4.translate')}</Button>
              <Button size="small" onClick={() => setConvStatus(c.status === 'closed' ? 'pending' : 'closed')}>{c.status === 'closed' ? t('p4.reopen') : t('p4.close')}</Button>
              <Button size="small" danger onClick={() => setConvStatus('spam')}>{t('p4.spam')}</Button></Space>}>
            <div style={{ maxHeight: 420, overflow: 'auto', marginBottom: 12 }}>
              {detail.messages.map((m: any) => (
                <Card key={m.id} size="small" style={{ marginBottom: 8, background: m.direction === 'note' ? '#fffbe6' : m.direction === 'out' ? '#f6ffed' : '#fff', marginLeft: m.direction === 'in' ? 0 : 40 }}
                  title={<Space size={4}><Tag>{t(`p4.dir_${m.direction}`)}</Tag>{m.direction !== 'note' && <span>{m.subject}</span>}
                    {m.sent_by !== 'customer' && m.direction !== 'note' && <Tag>{t(`p4.by_${m.sent_by}`, { defaultValue: m.sent_by })}</Tag>}
                    {m.status === 'blocked' && <Tag color="red">{t('p4.blocked')}</Tag>}
                    {(m.detail?.sensitive || []).map((s: string) => <Tag key={s} color="orange">{t(`p4.topic_${s}`)}</Tag>)}</Space>}
                  extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>{new Date(m.created_at).toLocaleString()}</Typography.Text>}>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{m.body}</pre>
                  {m.body_translated && <pre style={{ whiteSpace: 'pre-wrap', margin: '8px 0 0', color: '#888', fontFamily: 'inherit', borderTop: '1px dashed #ddd', paddingTop: 6 }}>{m.body_translated}</pre>}
                </Card>))}
            </div>
            {can('inbox.reply') && c.status !== 'spam' && <>
              <Space style={{ marginBottom: 8 }}>
                <Button size="small" loading={busy} onClick={() => suggest()}>{t('p4.aiDraft')}</Button>
                <Select size="small" style={{ width: 120 }} placeholder={t('p4.tone')} onChange={(v) => suggest(v)}
                  options={['professional', 'friendly', 'concise'].map((v) => ({ value: v, label: t(`p4.tone_${v}`) }))} />
              </Space>
              {sug?.needs_human && <Alert type="warning" showIcon style={{ marginBottom: 8 }} message={t('p4.sensitiveWarn', { topics: (sug.sensitive_zh || []).join('、') || t('p4.lowConfidence') })} />}
              {sug && sug.drafts.length > 1 && <Radio.Group style={{ marginBottom: 8 }} onChange={(e) => setDraft(sug.drafts[e.target.value].body)} defaultValue={0}
                options={sug.drafts.map((_: any, i: number) => ({ value: i, label: `${t('p4.draft')} ${i + 1}` }))} />}
              <Input.TextArea rows={6} value={draft} onChange={(e) => { setDraft(e.target.value); setCheck(undefined) }} placeholder={t('p4.replyPh')} />
              {bilingual && <Card size="small" style={{ marginTop: 8 }} title={t('p4.bilingual')}><pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{bilingual}</pre></Card>}
              {check && <div style={{ marginTop: 8 }}>
                {check.gate.decision === 'deny' && <Alert type="error" showIcon message={t('p4.gateDeny', { codes: check.gate.blocks.map((b: any) => b.code).join(', ') })} />}
                {check.qa.blocking.length > 0 && <Alert type="error" showIcon message={t('p4.forbidden', { items: check.qa.blocking.flatMap((b: any) => b.items).join('；') })} />}
                {check.gate.decision === 'allow' && !check.qa.blocking.length && <Alert type="success" showIcon message={t('p4.checkOk')}
                  description={check.qa.warnings.length ? `${t('p4.warnings')}：${check.qa.warnings.map((w: any) => t(`p3.qa_${w.code}`, { defaultValue: w.code })).join('、')}` : undefined} />}
              </div>}
              <Space style={{ marginTop: 8 }}>
                <Button disabled={!draft.trim()} onClick={precheck}>{t('p4.precheck')}</Button>
                <Button type="primary" loading={busy} disabled={!check || check.gate.decision === 'deny' || check.qa.blocking.length > 0} onClick={send}>{t('p4.send')}</Button>
                <Typography.Text type="secondary">{t('p4.humanSends')}</Typography.Text>
              </Space>
            </>}
            <Space.Compact style={{ width: '100%', marginTop: 12 }}>
              <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t('p4.notePh')} maxLength={5000} />
              <Button disabled={!note.trim()} onClick={addNote}>{t('p4.addNote')}</Button>
            </Space.Compact>
          </Card>)}
      </Col>
    </Row>
  )
}
