/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Checkbox, Col, Form, Input, InputNumber, Row, Space, Table, Tabs, Tag, TimePicker, Typography, message } from 'antd'
import dayjs from 'dayjs'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const CODES = ['N-01', 'N-03', 'N-05', 'N-10', 'N-15', 'N-17', 'N-18', 'N-20', 'N-21', 'N-22', 'N-23', 'N-26', 'N-27', 'N-28', 'N-29', 'N-31', 'N-32', 'N-33']
const SCOPES = ['companies', 'contacts', 'accounts', 'opportunities', 'quotes', 'proforma_invoices', 'conversations', 'messages', 'inquiries', 'handoffs', 'sequences', 'tasks', 'knowledge_items', 'icps', 'suppressions', 'audit_logs']

function Mine() {
  const { t } = useTranslation()
  const [s, setS] = useState<any>()
  const [setup, setSetup] = useState<any>()
  const [code, setCode] = useState('')
  const load = () => { api.get('/me/security').then(r => setS(r.data)) }
  useEffect(load, [])
  if (!s) return null
  const muted: string[] = s.notify_prefs?.muted || []
  const dnd: string[] | null = s.notify_prefs?.dnd || null
  const savePrefs = async (m: string[], d: string[] | null) => { await api.put('/me/notify-prefs', { muted: m, dnd: d }); message.success(t('b1.saved')); load() }
  return <Row gutter={16}>
    <Col span={12}>
      <Card size="small" title={t('b1.twoFactor')} style={{ marginBottom: 12 }}>
        {s.totp_enabled ? <Space><Tag color="green">{t('b1.enabled')}</Tag><Input style={{ width: 120 }} placeholder={t('b1.mfaCode')} value={code} onChange={e => setCode(e.target.value)} />
          <Button danger onClick={async () => { await api.post('/me/mfa:disable', { code }); setCode(''); load() }}>{t('b1.disable')}</Button></Space>
          : setup ? <><Alert type="info" message={t('b1.scanHint')} description={<><Typography.Text copyable>{setup.secret}</Typography.Text><br /><Typography.Text type="secondary" copyable style={{ fontSize: 12 }}>{setup.otpauth_uri}</Typography.Text></>} />
            <Space style={{ marginTop: 8 }}><Input style={{ width: 120 }} placeholder={t('b1.mfaCode')} value={code} onChange={e => setCode(e.target.value)} />
              <Button type="primary" onClick={async () => { await api.post('/me/mfa:enable', { code }); setSetup(undefined); setCode(''); load() }}>{t('b1.enable')}</Button></Space></>
            : <Button onClick={() => api.post('/me/mfa:setup').then(r => setSetup(r.data))}>{t('b1.setup2fa')}</Button>}
      </Card>
      <Card size="small" title={t('b1.changePw')}>
        <Form layout="vertical" onFinish={async v => { const r = await api.post('/me/password', v); message.success(t('b1.pwChanged', { n: r.data.other_sessions_revoked })) }}>
          <Form.Item name="old_password" label={t('b1.oldPw')} rules={[{ required: true }]}><Input.Password /></Form.Item>
          <Form.Item name="new_password" label={t('b1.newPw')} extra={t('b1.pwRule')} rules={[{ required: true, min: 10 }]}><Input.Password /></Form.Item>
          <Button type="primary" htmlType="submit">{t('b1.save')}</Button>
        </Form>
      </Card>
    </Col>
    <Col span={12}>
      <Card size="small" title={t('b1.sessions')} extra={<Button size="small" onClick={() => api.post('/me/sessions:revoke-others').then(load)}>{t('b1.revokeOthers')}</Button>} style={{ marginBottom: 12 }}>
        <Table rowKey="id" size="small" pagination={false} dataSource={s.sessions} columns={[
          { title: 'IP', dataIndex: 'ip', render: (v, r: any) => <>{v} {r.current && <Tag color="green">{t('b1.current')}</Tag>}</> },
          { title: t('b1.device'), dataIndex: 'user_agent', ellipsis: true },
          { title: t('b1.lastSeen'), dataIndex: 'last_seen_at', render: v => new Date(v).toLocaleString() },
          { title: '', render: (_, r: any) => !r.current && <a onClick={() => api.post(`/me/sessions/${r.id}:revoke`).then(load)}>{t('b1.signOut')}</a> },
        ]} />
      </Card>
      <Card size="small" title={t('b1.notifyPrefs')}>
        <Typography.Paragraph type="secondary">{t('b1.prefsHint')}</Typography.Paragraph>
        <Checkbox.Group value={CODES.filter(c => !muted.includes(c))} onChange={v => savePrefs(CODES.filter(c => !v.includes(c)), dnd)}
          options={CODES.map(c => ({ value: c, label: `${c} ${t('b1.n_' + c)}`, disabled: s.non_optional.includes(c) }))} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr' }} />
        <Space style={{ marginTop: 12 }}>{t('b1.dnd')}
          <TimePicker.RangePicker format="HH:mm" value={dnd ? [dayjs(dnd[0], 'HH:mm'), dayjs(dnd[1], 'HH:mm')] : null}
            onChange={v => savePrefs(muted, v && v[0] && v[1] ? [v[0].format('HH:mm'), v[1].format('HH:mm')] : null)} /></Space>
      </Card>
    </Col>
  </Row>
}

function Admin() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [grants, setGrants] = useState<any[]>([])
  const [exps, setExps] = useState<any[]>([])
  const [scope, setScope] = useState<string[]>([])
  const load = () => {
    if (can('security.manage')) api.get('/support-grants').then(r => setGrants(r.data))
    if (can('data.export')) api.get('/data-exports').then(r => setExps(r.data))
  }
  useEffect(load, [])
  const dl = async (url: string, name: string) => {
    const r = await api.get(url, { responseType: 'blob' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(r.data); a.download = name; a.click()
  }
  return <Row gutter={16}>
    {can('data.export') && <Col span={12}><Card size="small" title={t('b1.dataExport')}>
      <Typography.Paragraph type="secondary">{t('b1.exportHint')}</Typography.Paragraph>
      <Checkbox.Group value={scope} onChange={v => setScope(v as string[])} options={SCOPES.map(s => ({ value: s, label: t('b1.obj_' + s, { defaultValue: s }) }))} />
      <div style={{ margin: '8px 0' }}><Button type="primary" onClick={async () => { await api.post('/data-exports', { scope }); message.success(t('b1.exportQueued')); load() }}>{t('b1.startExport')}</Button>
        <Button style={{ marginLeft: 8 }} onClick={load}>{t('b1.refresh')}</Button></div>
      <Table rowKey="id" size="small" dataSource={exps} pagination={{ pageSize: 5 }} columns={[
        { title: t('b1.createdAt'), dataIndex: 'created_at', render: v => new Date(v).toLocaleString() },
        { title: t('b1.status'), dataIndex: 'status', render: v => <Tag>{t('b1.es_' + v)}</Tag> },
        { title: t('b1.size'), dataIndex: 'size_bytes', render: v => v ? `${(v / 1024).toFixed(1)} KB` : '—' },
        { title: '', render: (_, r: any) => r.status === 'ready' && <a onClick={() => dl(`/data-exports/${r.id}/download`, 'export.zip')}>{t('b1.download')}</a> },
      ]} />
    </Card></Col>}
    {can('security.manage') && <Col span={12}>
      <Card size="small" title={t('b1.supportAccess')} style={{ marginBottom: 12 }}>
        <Typography.Paragraph type="secondary">{t('b1.supportHint')}</Typography.Paragraph>
        <Form layout="inline" onFinish={async v => { await api.post('/support-grants', v); load() }} initialValues={{ hours: 24 }}>
          <Form.Item name="hours" rules={[{ required: true }]}><InputNumber min={1} max={72} addonAfter={t('b1.hours')} /></Form.Item>
          <Form.Item name="reason" rules={[{ required: true, min: 2 }]}><Input placeholder={t('b1.reason')} /></Form.Item>
          <Form.Item name="ticket_ref"><Input placeholder={t('b1.ticket')} style={{ width: 100 }} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('b1.grant')}</Button>
        </Form>
        <Table rowKey="id" size="small" style={{ marginTop: 8 }} dataSource={grants} pagination={{ pageSize: 5 }} columns={[
          { title: t('b1.reason'), dataIndex: 'reason' }, { title: t('b1.expiresAt'), dataIndex: 'expires_at', render: v => new Date(v).toLocaleString() },
          { title: t('b1.status'), dataIndex: 'active', render: v => <Tag color={v ? 'green' : 'default'}>{v ? t('b1.activeNow') : t('b1.ended')}</Tag> },
          { title: '', render: (_, r: any) => r.active && <a onClick={() => api.post(`/support-grants/${r.id}:revoke`).then(load)}>{t('b1.revoke')}</a> },
        ]} />
      </Card>
      <Card size="small" title={t('b1.auditExport')}><Button onClick={() => dl('/audit-logs.csv', 'audit-logs.csv')}>{t('b1.downloadCsv')}</Button></Card>
    </Col>}
  </Row>
}

export default function SecurityPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  return <Tabs items={[{ key: 'me', label: t('b1.mySecurity'), children: <Mine /> },
    (can('security.manage') || can('data.export')) && { key: 'admin', label: t('b1.tenantSecurity'), children: <Admin /> }].filter(Boolean) as any} />
}
