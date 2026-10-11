/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, List, Select, Space, Table, Tabs, Tag, Upload, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'
import OAuthConnect from '../OAuthConnect'

function Sites() {
  const { t } = useTranslation()
  const [items, setItems] = useState<any[]>([])
  const load = () => api.get('/sites').then(r => setItems(r.data))
  useEffect(() => { load() }, [])
  return <>
    <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('b78.siteNote')} />
    <Form layout="inline" onFinish={v => api.post('/sites', v).then(r => { message.success(r.data.preview); load() })}
      initialValues={{ lang: 'en' }} style={{ marginBottom: 12 }}>
      <Form.Item name="slug" rules={[{ required: true }]}><Input placeholder="slug" /></Form.Item>
      <Form.Item name="title" rules={[{ required: true }]}><Input placeholder={t('b78.title')} /></Form.Item>
      <Form.Item name="lang"><Select style={{ width: 90 }} options={[{ value: 'en', label: 'EN' }, { value: 'zh', label: '中文' }]} /></Form.Item>
      <Button type="primary" htmlType="submit">{t('b78.create')}</Button>
    </Form>
    <Table size="small" rowKey="id" dataSource={items} columns={[
      { title: 'Slug', dataIndex: 'slug' }, { title: t('b78.title'), dataIndex: 'title' }, { title: t('b78.status'), dataIndex: 'status' },
      { title: '', render: (_: any, r: any) => <Space>
        <a onClick={() => api.post(`/sites/${r.id}/products:generate`).then(x => message.success(`${x.data.pages} pages`))}>{t('b78.genProducts')}</a>
        <a onClick={() => api.post(`/sites/${r.id}:publish`).then(x => message.success(x.data.url))}>{t('b78.publish')}</a>
        <a href={`/s/${r.slug}/`} target="_blank" rel="noreferrer">{t('b78.open')}</a>
      </Space> }]} />
  </>
}

function Deliverability() {
  const { t } = useTranslation()
  const [seeds, setSeeds] = useState<any[]>([])
  const [mbs, setMbs] = useState<any[]>([])
  useEffect(() => { api.get('/seed-tests').then(r => setSeeds(r.data)); api.get('/mailboxes').then(r => setMbs(r.data.items || r.data || [])) }, [])
  return <>
    <Space style={{ marginBottom: 12 }}>
      <OAuthConnect provider="google">{t('b78.oauthGoogle')}</OAuthConnect>
      <OAuthConnect provider="microsoft">{t('b78.oauthMs')}</OAuthConnect>
      <Button type="primary" onClick={() => {
        const mb = mbs[0]?.id
        api.post('/seed-tests', { mailbox_id: mb, subject: 'Seed test', body: 'Hello from seed test' }).then(r => { message.success(`inbox ${Math.round((r.data.inbox_rate || 0) * 100)}%`); api.get('/seed-tests').then(x => setSeeds(x.data)) })
      }}>{t('b78.runSeed')}</Button>
      {mbs[0] && <Button onClick={() => api.post(`/mailboxes/${mbs[0].id}/warmup:tick`).then(r => message.success(JSON.stringify(r.data)))}>{t('b78.warmup')}</Button>}
    </Space>
    <Alert type="info" showIcon message={t('b78.delivNote')} style={{ marginBottom: 12 }} />
    <Table size="small" rowKey="id" dataSource={seeds} columns={[{ title: t('b78.subject'), dataIndex: 'subject' }, { title: t('b78.inboxRate'), dataIndex: 'inbox_rate', render: (v: number) => v == null ? '—' : `${Math.round(v * 100)}%` }, { title: t('b78.status'), dataIndex: 'status' }]} />
  </>
}

function Developer() {
  const { t } = useTranslation()
  const [keys, setKeys] = useState<any[]>([])
  const [wh, setWh] = useState<any[]>([])
  const [mcp, setMcp] = useState<any>()
  const load = () => { api.get('/developer/api-keys').then(r => setKeys(r.data)); api.get('/developer/webhooks').then(r => setWh(r.data)); api.get('/developer/mcp').then(r => setMcp(r.data)) }
  useEffect(() => { load() }, [])
  return <>
    <Space style={{ marginBottom: 12 }}>
      <Button type="primary" onClick={() => api.post('/developer/api-keys', { name: 'default', scopes: ['read', 'write'] }).then(r => { message.success(r.data.key); load() })}>{t('b78.newKey')}</Button>
      <Button onClick={() => api.post('/developer/webhooks', { url: 'http://localhost/hooks/mock', events: ['deal.won', 'inquiry.created'] }).then(load)}>{t('b78.newWh')}</Button>
      <Button onClick={() => api.post('/developer/webhooks/test').then(r => message.success(`${r.data.deliveries} deliveries`))}>{t('b78.testWh')}</Button>
    </Space>
    <Table size="small" rowKey="id" title={() => 'API Keys'} dataSource={keys} columns={[{ title: t('b78.name'), dataIndex: 'name' }, { title: 'Prefix', dataIndex: 'prefix' }, { title: t('b78.status'), render: (_: any, r: any) => r.revoked ? <Tag>revoked</Tag> : <Tag color="green">active</Tag> },
      { title: '', render: (_: any, r: any) => !r.revoked && <a onClick={() => api.delete(`/developer/api-keys/${r.id}`).then(load)}>{t('b78.revoke')}</a> }]} />
    <Table size="small" style={{ marginTop: 12 }} rowKey="id" title={() => 'Webhooks'} dataSource={wh} columns={[{ title: 'URL', dataIndex: 'url' }, { title: 'Events', dataIndex: 'events', render: (v: any) => (v || []).join(', ') }, { title: 'Fails', dataIndex: 'fail_count' }]} />
    {mcp && <Card size="small" style={{ marginTop: 12 }} title="MCP Stub"><pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(mcp, null, 2)}</pre></Card>}
  </>
}

function Success() {
  const { t } = useTranslation()
  const [academy, setAcademy] = useState<any[]>([])
  const [roadmap, setRoadmap] = useState<any[]>([])
  const [subs, setSubs] = useState<any[]>([])
  useEffect(() => { api.get('/academy').then(r => setAcademy(r.data)); api.get('/roadmap').then(r => setRoadmap(r.data)); api.get('/subprocessors').then(r => setSubs(r.data)) }, [])
  return <>
    <Card size="small" title={t('b78.academy')} style={{ marginBottom: 12 }}>
      <List dataSource={academy} renderItem={(a: any) => <List.Item><div><b>{a.title}</b><div style={{ color: '#888' }}>{a.category}</div></div></List.Item>} />
    </Card>
    <Card size="small" title={t('b78.roadmap')} style={{ marginBottom: 12 }}>
      <Table size="small" rowKey="id" pagination={false} dataSource={roadmap} columns={[{ title: t('b78.title'), dataIndex: 'title' }, { title: t('b78.status'), dataIndex: 'status' }, { title: 'Q', dataIndex: 'quarter' }]} />
    </Card>
    <Card size="small" title={t('b78.subprocessors')}>
      <Table size="small" rowKey="name" pagination={false} dataSource={subs} columns={[{ title: t('b78.name'), dataIndex: 'name' }, { title: t('b78.purpose'), dataIndex: 'purpose' }, { title: t('b78.location'), dataIndex: 'location' }]} />
    </Card>
    <Card size="small" title={t('b78.feedback')} style={{ marginTop: 12 }}>
      <Form layout="vertical" onFinish={v => api.post('/feedback', v).then(() => message.success('ok'))}>
        <Form.Item name="title" rules={[{ required: true }]}><Input placeholder={t('b78.title')} /></Form.Item>
        <Form.Item name="body" rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="kind" initialValue="idea"><Select options={[{ value: 'idea', label: 'Idea' }, { value: 'bug', label: 'Bug' }, { value: 'praise', label: 'Praise' }]} /></Form.Item>
        <Button type="primary" htmlType="submit">{t('b78.submit')}</Button>
      </Form>
    </Card>
  </>
}

function Erp() {
  const { t } = useTranslation()
  const [conns, setConns] = useState<any[]>([])
  const load = () => api.get('/erp/connections').then(r => setConns(r.data))
  useEffect(() => { load() }, [])
  return <>
    <Button style={{ marginBottom: 12 }} type="primary" onClick={() => api.post('/erp/connections', { name: 'CSV', kind: 'csv' }).then(load)}>{t('b78.addErp')}</Button>
    <Table size="small" rowKey="id" dataSource={conns} columns={[{ title: t('b78.name'), dataIndex: 'name' }, { title: t('b78.kind'), dataIndex: 'kind' },
      { title: '', render: (_: any, r: any) => <Space>
        <a onClick={() => api.post(`/erp/connections/${r.id}/export`, null, { params: { object_type: 'account' } }).then(x => message.success(`${x.data.rows} rows`))}>{t('b78.export')}</a>
        <Upload showUploadList={false} customRequest={({ file }: any) => { const fd = new FormData(); fd.append('file', file); api.post(`/erp/connections/${r.id}/import?object_type=product`, fd).then(x => message.success(`${x.data.rows} imported`)) }}><a>{t('b78.importCsv')}</a></Upload>
      </Space> }]} />
  </>
}

function Im() {
  const { t } = useTranslation()
  const [chs, setChs] = useState<any[]>([])
  const load = () => api.get('/im/channels').then(r => setChs(r.data))
  useEffect(() => { load() }, [])
  return <>
    <Space style={{ marginBottom: 12 }}>
      {['wecom', 'dingtalk', 'feishu', 'slack'].map(p => <Button key={p} onClick={() => api.post('/im/channels', { provider: p, name: p }).then(load)}>{p}</Button>)}
    </Space>
    <Table size="small" rowKey="id" dataSource={chs} columns={[{ title: t('b78.provider'), dataIndex: 'provider' }, { title: t('b78.name'), dataIndex: 'name' }, { title: t('b78.status'), dataIndex: 'status' },
      { title: '', render: (_: any, r: any) => <a onClick={() => api.post(`/im/channels/${r.id}/messages:inbound-mock`, { text: '怎么绑定邮箱？' }).then(x => message.info(x.data.reply))}>{t('b78.tryIm')}</a> }]} />
  </>
}

export default function PlatformPage() {
  const { t } = useTranslation()
  const { can } = useAuth() as any
  const tabs = [
    can('kb.manage') && { key: 'site', label: t('b78.tabSite'), children: <Sites /> },
    can('mailbox.manage') && { key: 'deliv', label: t('b78.tabDeliv'), children: <Deliverability /> },
    can('org.manage') && { key: 'dev', label: t('b78.tabDev'), children: <Developer /> },
    can('org.manage') && { key: 'erp', label: t('b78.tabErp'), children: <Erp /> },
    can('org.manage') && { key: 'im', label: t('b78.tabIm'), children: <Im /> },
    { key: 'ok', label: t('b78.tabSuccess'), children: <Success /> },
  ].filter(Boolean) as any[]
  return <Card title={t('menu.platform')}><Tabs items={tabs} /></Card>
}
