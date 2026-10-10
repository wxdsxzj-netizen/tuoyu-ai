/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, List, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

function WidgetList() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [edit, setEdit] = useState<any>()
  const [form] = Form.useForm()
  const load = () => api.get('/widgets').then(r => setRows(r.data))
  useEffect(() => { load(); api.get('/users').then(r => setUsers(r.data.items || r.data)).catch(() => setUsers([])) }, [])
  const open = (w?: any) => { setEdit(w || {}); form.setFieldsValue(w ? { ...w, origins: (w.allowed_origins || []).join(','), owner: w.routing?.owner_id } : { kind: 'form', cookie_banner: true }) }
  const save = async () => {
    const v = await form.validateFields()
    const body = { name: v.name, kind: v.kind, allowed_origins: (v.origins || '').split(',').map((s: string) => s.trim()).filter(Boolean),
      consent_text: v.consent_text || null, cookie_banner: !!v.cookie_banner, ai_reception: !!v.ai_reception, greeting: v.greeting || null,
      routing: v.owner ? { owner_id: v.owner } : {}, active: v.active !== false }
    if (edit?.id) await api.put(`/widgets/${edit.id}`, body); else await api.post('/widgets', body)
    message.success(t('b1.saved')); setEdit(undefined); load()
  }
  const snippet = async (w: any) => {
    const r = await api.get(`/widgets/${w.id}/snippet`)
    Modal.info({ width: 680, title: t('b4.snippet'), content: <>
      <Typography.Paragraph>{t('b4.snippetScript')}</Typography.Paragraph><Typography.Paragraph copyable code>{r.data.html}</Typography.Paragraph>
      <Typography.Paragraph>{t('b4.snippetIframe')}</Typography.Paragraph><Typography.Paragraph copyable code>{r.data.iframe}</Typography.Paragraph>
      <Alert type="info" showIcon message={t('b4.noExternal')} /></> })
  }
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b4.widgetIntro')} />
    {can('widget.manage') && <Button type="primary" onClick={() => open()} style={{ marginBottom: 12 }}>{t('b4.newWidget')}</Button>}
    <Table rowKey="id" size="small" dataSource={rows} pagination={false} columns={[
      { title: t('b4.name'), dataIndex: 'name' },
      { title: t('b4.kind'), dataIndex: 'kind', render: k => <Tag>{t('b4.kind_' + k)}</Tag> },
      { title: t('b4.origins'), dataIndex: 'allowed_origins', render: (o: string[]) => o?.length ? o.join(', ') : <Tag color="orange">{t('b4.anyOrigin')}</Tag> },
      { title: t('b4.aiReception'), dataIndex: 'ai_reception', render: v => v ? <Tag color="green">{t('b4.on')}</Tag> : <Tag>{t('b4.off')}</Tag> },
      { title: t('b4.status'), dataIndex: 'active', render: v => v ? <Tag color="green">{t('b4.active')}</Tag> : <Tag>{t('b4.inactive')}</Tag> },
      { title: '', render: (_: any, w: any) => <Space>{can('widget.manage') && <><Button size="small" onClick={() => open(w)}>{t('b4.edit')}</Button>
        <Button size="small" onClick={() => snippet(w)}>{t('b4.snippet')}</Button></>}
        <Button size="small" href={`/w/${w.public_key}`} target="_blank">{t('b4.preview')}</Button></Space> },
    ]} />
    <Modal open={!!edit} title={edit?.id ? t('b4.edit') : t('b4.newWidget')} onOk={save} onCancel={() => setEdit(undefined)} width={640}>
      <Form form={form} layout="vertical">
        <Form.Item name="name" label={t('b4.name')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="kind" label={t('b4.kind')}><Select options={['form', 'chat'].map(k => ({ value: k, label: t('b4.kind_' + k) }))} /></Form.Item>
        <Form.Item name="origins" label={t('b4.origins')} extra={t('b4.originsHelp')}><Input placeholder="nbvalve.com, shop.nbvalve.com" /></Form.Item>
        <Form.Item name="owner" label={t('b4.routeTo')}><Select allowClear options={users.map((u: any) => ({ value: u.id, label: u.name }))} /></Form.Item>
        <Form.Item name="greeting" label={t('b4.greeting')}><Input /></Form.Item>
        <Form.Item name="consent_text" label={t('b4.consentText')} extra={t('b4.consentHelp')}><Input.TextArea rows={2} /></Form.Item>
        <Space size={24}>
          <Form.Item name="ai_reception" label={t('b4.aiReception')} valuePropName="checked" extra={t('b4.aiHelp')}><Switch /></Form.Item>
          <Form.Item name="cookie_banner" label={t('b4.cookieBanner')} valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="active" label={t('b4.active')} valuePropName="checked" initialValue><Switch /></Form.Item>
        </Space>
      </Form>
    </Modal>
  </>
}

function ChatDesk() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [rows, setRows] = useState<any[]>([])
  const [cur, setCur] = useState<any>()
  const [text, setText] = useState('')
  const load = () => api.get('/widget-sessions').then(r => { setRows(r.data); if (cur) setCur(r.data.find((x: any) => x.id === cur.id)) })
  useEffect(() => { load(); const i = setInterval(load, 8000); return () => clearInterval(i) }, [cur?.id])
  const send = async () => { if (!text.trim()) return; await api.post(`/widget-sessions/${cur.id}/reply`, { text }); setText(''); load() }
  return <div style={{ display: 'flex', gap: 12 }}>
    <Card size="small" style={{ width: 300 }} title={t('b4.sessions')}>
      <List size="small" dataSource={rows} renderItem={(s: any) => <List.Item onClick={() => setCur(s)} style={{ cursor: 'pointer', background: cur?.id === s.id ? '#e6f4ff' : undefined }}>
        <List.Item.Meta title={<>{s.visitor} {s.country && <Tag>{s.country}</Tag>}</>} description={<Tag color={{ ai: 'blue', human: 'red', closed: 'default' }[s.mode as string]}>{t('b4.mode_' + s.mode)}</Tag>} />
      </List.Item>} />
    </Card>
    <Card size="small" style={{ flex: 1 }} title={cur ? cur.visitor : t('b4.pickSession')}
      extra={cur && cur.mode !== 'closed' && <Button size="small" onClick={async () => { await api.post(`/widget-sessions/${cur.id}:close`); load() }}>{t('b4.closeSession')}</Button>}>
      {cur && <>
        <div style={{ maxHeight: 420, overflow: 'auto', marginBottom: 8 }}>{(cur.messages || []).map((m: any, i: number) =>
          <div key={i} style={{ textAlign: m.from === 'visitor' ? 'left' : 'right', margin: '6px 0' }}>
            <Tag color={{ visitor: 'default', bot: 'blue', agent: 'green' }[m.from as string]}>{t('b4.from_' + m.from)}{m.name ? ' · ' + m.name : ''}</Tag>
            <div style={{ display: 'inline-block', maxWidth: '70%', background: '#f5f5f5', padding: '4px 8px', borderRadius: 6 }}>{m.text}</div>
            {m.handoff_reason && <div><Tag color="orange">{t('b4.handoff')}：{m.handoff_reason}</Tag></div>}
          </div>)}</div>
        {can('inbox.reply') && cur.mode !== 'closed' && <Space.Compact style={{ width: '100%' }}>
          <Input value={text} onChange={e => setText(e.target.value)} onPressEnter={send} placeholder={t('b4.replyPh')} />
          <Button type="primary" onClick={send}>{t('b4.send')}</Button></Space.Compact>}
        <div style={{ color: '#888', marginTop: 6 }}>{t('b4.chatNote')}</div>
      </>}
    </Card>
  </div>
}

export default function WidgetsPage() {
  const { t } = useTranslation()
  return <Card title={t('menu.widgets')}><Tabs items={[{ key: 'w', label: t('b4.widgets'), children: <WidgetList /> }, { key: 'c', label: t('b4.chatDesk'), children: <ChatDesk /> }]} /></Card>
}
