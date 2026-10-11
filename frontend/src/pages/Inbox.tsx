/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Badge, Button, Card, Col, Empty, Input, List, Radio, Row, Select, Space, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth'

const CAT_COLOR: Record<string, string> = { inquiry: 'green', positive: 'green', sample: 'cyan', meeting: 'blue', referral: 'purple', not_interested: 'default', unsubscribe: 'red', auto_reply: 'gold' }

export default function InboxPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('c')
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
  const [openError, setOpenError] = useState<string>()
  const generation = useRef(0)
  const draftVersion = useRef(0)
  const inflight = useRef(false)
  const changeDraft = (value: string) => {
    draftVersion.current += 1
    setDraft(value); setCheck(undefined); setBilingual(null)
  }
  const load = useCallback(async () => {
    const r = await api.get('/conversations', { params: { status, size: 100 } })
    setRows(r.data.items)
  }, [status])
  const open = useCallback(async (id: string) => {
    const requestGeneration = ++generation.current
    draftVersion.current += 1
    inflight.current = false
    setCur(id); setDetail(undefined); setSug(undefined); setCheck(undefined); setDraft(''); setBilingual(null); setNote(''); setBusy(false); setOpenError(undefined)
    try {
      const r = await api.get(`/conversations/${id}`)
      if (generation.current === requestGeneration && r.data.conversation.id === id) setDetail(r.data)
    } catch (error: any) {
      if (generation.current === requestGeneration) setOpenError([403, 404].includes(error.response?.status) ? '会话不存在或无权查看。' : '会话加载失败，请重试。')
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (selectedId) open(selectedId)
    else { generation.current++; setCur(undefined); setDetail(undefined); setOpenError(undefined) }
  }, [selectedId, open])
  useEffect(() => () => { generation.current += 1 }, [])

  const c = detail?.conversation.id === cur ? detail?.conversation : undefined
  const begin = () => {
    if (!c || inflight.current) return undefined
    inflight.current = true; setBusy(true)
    return { id: c.id as string, generation: generation.current, version: draftVersion.current }
  }
  const current = (op: { generation: number }) => generation.current === op.generation
  const finish = (op: { generation: number }) => {
    if (current(op)) { inflight.current = false; setBusy(false) }
  }

  const suggest = async (tone = 'professional') => {
    const op = begin(); if (!op) return
    try {
      const r = await api.post(`/conversations/${op.id}:suggest`, null, { params: { tone } })
      if (current(op) && draftVersion.current === op.version) {
        setSug(r.data)
        if (r.data.drafts[0]) changeDraft(r.data.drafts[0].body)
      }
    } catch { /* API client feedback; preserve the current draft. */ } finally { finish(op) }
  }
  const precheck = async () => {
    const op = begin(); if (!op) return
    const body = draft, language = c.language || 'en'
    try {
      const r = await api.post(`/conversations/${op.id}:check`, { body })
      if (!current(op) || draftVersion.current !== op.version) return
      setCheck({ ...r.data, checkedBody: body })
      if (/[\u4e00-\u9fff]/.test(body)) {
        const tr = await api.post('/inbox/translate', { text: body, target: language })
        if (current(op) && draftVersion.current === op.version) setBilingual(tr.data.available ? tr.data.text : null)
      }
    } catch { /* Do not authorize a draft when the check fails. */ } finally { finish(op) }
  }
  const send = async () => {
    if (!check || check.checkedBody !== draft || check.gate.decision === 'deny' || check.qa.blocking.length) return
    const op = begin(); if (!op) return
    try {
      await api.post(`/conversations/${op.id}:reply`, { body: draft })
      if (current(op)) { message.success(t('p4.sent')); await open(op.id) }
      await load()
    } catch { /* Preserve the selected conversation and draft for retry. */ } finally { finish(op) }
  }
  const mutateConversation = async (action: (id: string) => Promise<unknown>) => {
    const op = begin(); if (!op) return
    try {
      await action(op.id)
      if (current(op)) await open(op.id)
      await load()
    } catch { /* API client feedback. */ } finally { finish(op) }
  }
  const addNote = () => mutateConversation((id) => api.post(`/conversations/${id}/notes`, { body: note }))
  const setConvStatus = (s: string) => mutateConversation((id) => api.patch(`/conversations/${id}`, { status: s }))

  return (
    <Row gutter={16}>
      <Col span={8}>
        <Card size="small" title={t('p4.inbox')} extra={<Radio.Group size="small" value={status ?? ''} onChange={(e) => setStatus(e.target.value || undefined)}
          options={[{ value: '', label: t('p4.all') }, { value: 'unread', label: t('p4.cs_unread') }, { value: 'pending', label: t('p4.cs_pending') }, { value: 'closed', label: t('p4.cs_closed') }]} optionType="button" />}>
          <List size="small" dataSource={rows} locale={{ emptyText: <Empty description={t('p4.noConv')} /> }} renderItem={(r: any) => (
            <List.Item onClick={() => setParams(previous => { const next = new URLSearchParams(previous); next.set('c', r.id); return next })} style={{ cursor: 'pointer', background: r.id === cur ? '#e6f4ff' : undefined, paddingLeft: 8 }}>
              <List.Item.Meta
                title={<Space>{r.status === 'unread' && <Badge status="processing" />}<span>{r.peer_email}</span></Space>}
                description={<Space wrap size={4}><Typography.Text ellipsis style={{ maxWidth: 180 }}>{r.subject}</Typography.Text>
                  {r.last_category && <Tag color={CAT_COLOR[r.last_category]}>{t(`p3.rc_${r.last_category}`, { defaultValue: r.last_category })}</Tag>}
                  {r.needs_human && <Tag color="orange">{t('p4.needsHuman')}</Tag>}</Space>} />
            </List.Item>)} />
        </Card>
      </Col>
      <Col span={16}>
        {!c ? openError ? <Alert type="error" showIcon message={openError} action={<Button onClick={() => cur && open(cur)}>重试</Button>} /> : <Empty description={t('p4.pickConv')} /> : (
          <Card size="small" title={<Space>{c.peer_email}<Tag>{t(`p4.cs_${c.status}`)}</Tag>{detail.account && <Tag color="blue">{detail.account.name}</Tag>}
            {detail.handoff && <Tag color="green">{t('p4.handoff')}·{t(`p4.hs_${detail.handoff.status}`)}</Tag>}</Space>}
            extra={<Space><Button size="small" disabled={busy} onClick={() => mutateConversation((id) => api.post(`/conversations/${id}:translate`))}>{t('p4.translate')}</Button>
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
              {sug && sug.drafts.length > 1 && <Radio.Group style={{ marginBottom: 8 }} onChange={(e) => changeDraft(sug.drafts[e.target.value].body)} defaultValue={0}
                options={sug.drafts.map((_: any, i: number) => ({ value: i, label: `${t('p4.draft')} ${i + 1}` }))} />}
              <Input.TextArea rows={6} value={draft} onChange={(e) => changeDraft(e.target.value)} placeholder={t('p4.replyPh')} />
              {bilingual && <Card size="small" style={{ marginTop: 8 }} title={t('p4.bilingual')}><pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{bilingual}</pre></Card>}
              {check && <div style={{ marginTop: 8 }}>
                {check.gate.decision === 'deny' && <Alert type="error" showIcon message={t('p4.gateDeny', { codes: check.gate.blocks.map((b: any) => b.code).join(', ') })} />}
                {check.qa.blocking.length > 0 && <Alert type="error" showIcon message={t('p4.forbidden', { items: check.qa.blocking.flatMap((b: any) => b.items).join('；') })} />}
                {check.gate.decision === 'allow' && !check.qa.blocking.length && <Alert type="success" showIcon message={t('p4.checkOk')}
                  description={check.qa.warnings.length ? `${t('p4.warnings')}：${check.qa.warnings.map((w: any) => t(`p3.qa_${w.code}`, { defaultValue: w.code })).join('、')}` : undefined} />}
              </div>}
              <Space style={{ marginTop: 8 }}>
                <Button disabled={busy || !draft.trim()} onClick={precheck}>{t('p4.precheck')}</Button>
                <Button type="primary" loading={busy} disabled={!check || check.checkedBody !== draft || check.gate.decision === 'deny' || check.qa.blocking.length > 0} onClick={send}>{t('p4.send')}</Button>
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
