/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Drawer, Form, Input, InputNumber, Modal, Progress, Row, Select, Space, Table, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const LEVEL: Record<string, string> = { normal: 'green', watch: 'gold', slow: 'orange', paused: 'red' }
const ok = (s: string) => <Tag color={s === 'pass' ? 'green' : s === 'fail' ? 'red' : 'default'}>{s === 'pass' ? '✓' : s === 'fail' ? '✗' : '?'}</Tag>

export default function MailboxesPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [mbs, setMbs] = useState<any[]>([])
  const [doms, setDoms] = useState<any[]>([])
  const [binding, setBinding] = useState(false)
  const [hl, setHl] = useState<any>()
  const [dom, setDom] = useState<any>()
  const load = useCallback(async () => {
    const [a, b] = await Promise.all([api.get('/mailboxes'), api.get('/domains')]); setMbs(a.data); setDoms(b.data)
  }, [])
  useEffect(() => { load() }, [load])
  const manage = can('mailbox.manage')
  const openHealth = async (m: any) => setHl({ m, d: (await api.get(`/mailboxes/${m.id}/health`)).data })
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {mbs.some((m) => m.status === 'paused') && <Alert type="error" showIcon message={t('p3.breakerBanner')} />}
      <Space>
        {manage && <Button type="primary" onClick={() => setBinding(true)}>{t('p3.bindImap')}</Button>}
        {manage && <Button onClick={() => api.post('/mailboxes/oauth/google:start').catch(() => 0)}>{t('p3.bindGoogle')}</Button>}
        {manage && <Button onClick={() => api.post('/mailboxes/oauth/microsoft:start').catch(() => 0)}>{t('p3.bindMs')}</Button>}
      </Space>
      <Row gutter={[16, 16]}>
        {mbs.map((m) => (
          <Col key={m.id} xs={24} md={12} xl={8}>
            <Card title={m.address} extra={<Tag>{t(`p3.ms_${m.status}`)}</Tag>}
              actions={[<a key="h" onClick={() => openHealth(m)}>{t('p3.health')}</a>,
                manage && m.status === 'paused' && <a key="r" onClick={() => { let v = ''; Modal.confirm({ title: t('p3.resolve'),
                  content: <Input.TextArea onChange={(e) => { v = e.target.value }} />, onOk: async () => { await api.post(`/mailboxes/${m.id}:resolve`, { resolution: v }); load() } }) }}>{t('p3.resolve')}</a>,
                manage && m.status === 'cooling' && <a key="c" onClick={async () => { await api.post(`/mailboxes/${m.id}:recover`); load() }}>{t('p3.recover')}</a>,
                manage && <a key="t" onClick={async () => { const r = await api.post(`/mailboxes/${m.id}:test`); if (r.data.ok) message.success(t('p3.testOk')); else message.error(r.data.detail) }}>{t('p3.test')}</a>,
                manage && <a key="d" onClick={() => Modal.confirm({ title: t('p3.unbind'), onOk: async () => { await api.delete(`/mailboxes/${m.id}`); load() } })}>{t('p3.unbind')}</a>,
              ].filter(Boolean)}>
              <Space align="center" size="large">
                <Progress type="circle" size={72} percent={m.health_score} strokeColor={m.health_score >= 80 ? '#52c41a' : m.health_score >= 60 ? '#faad14' : '#f5222d'} format={(p) => p} />
                <Space direction="vertical" size={2}>
                  <span>{t('p3.level')}：<Tag color={LEVEL[m.health_level]}>{t(`p3.lv_${m.health_level}`)}</Tag>{m.health_note && <Typography.Text type="secondary">{m.health_note}</Typography.Text>}</span>
                  <span>{t('p3.todaySent')}：{m.today_sent} / {m.today_limit}</span>
                  {m.status === 'warming' && <span>{t('p3.warmup')}：<Progress percent={Math.round(Math.min(m.warmup_day, 21) / 21 * 100)} size="small" style={{ width: 120 }} /> {t('p3.dayN', { n: m.warmup_day })}</span>}
                  {m.cooldown_until && m.status === 'cooling' && <span>{t('p3.cooldownUntil')}：{new Date(m.cooldown_until).toLocaleString()}</span>}
                </Space>
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
      <Card title={t('p3.domains')}>
        <Table rowKey="id" size="small" pagination={false} dataSource={doms} columns={[
          { title: t('p3.domain'), dataIndex: 'domain' },
          { title: 'SPF', dataIndex: 'spf_status', render: ok }, { title: 'DKIM', dataIndex: 'dkim_status', render: ok },
          { title: 'DMARC', dataIndex: 'dmarc_status', render: ok }, { title: 'MX', dataIndex: 'mx_status', render: ok },
          { title: t('p3.status'), render: (_: any, d: any) => d.paused ? <Tag color="red">{t('p3.domainPaused')}</Tag> : d.dns_ok ? <Tag color="green">{t('p3.dnsOk')}</Tag> : <Tag color="red">{t('p3.dnsBad')}</Tag> },
          { title: t('p3.lastChecked'), dataIndex: 'last_checked_at', render: (v: string) => v ? new Date(v).toLocaleString() : '—' },
          { title: '', render: (_: any, d: any) => <Space>
            <a onClick={() => setDom(d)}>{t('p3.records')}</a>
            {manage && <a onClick={async () => { await api.post(`/domains/${d.id}:check`); load() }}>{t('p3.verify')}</a>}</Space> },
        ]} />
      </Card>
      <Modal open={binding} title={t('p3.bindImap')} footer={null} width={640} onCancel={() => setBinding(false)} destroyOnClose>
        <Alert style={{ marginBottom: 12 }} type="info" showIcon message={t('p3.credHint')} />
        <Form layout="vertical" initialValues={{ smtp_port: 587, smtp_security: 'starttls', imap_port: 993, imap_ssl: true, daily_cold_limit: 40 }}
          onFinish={async (v) => { await api.post('/mailboxes', v); setBinding(false); load() }}>
          <Row gutter={12}>
            <Col span={12}><Form.Item name="address" label={t('p3.address')} rules={[{ required: true, type: 'email' }]}><Input /></Form.Item></Col>
            <Col span={12}><Form.Item name="display_name" label={t('p3.displayName')}><Input /></Form.Item></Col>
            <Col span={10}><Form.Item name="smtp_host" label={t('p3.smtpHost')} rules={[{ required: true }]}><Input /></Form.Item></Col>
            <Col span={6}><Form.Item name="smtp_port" label={t('p3.port')}><InputNumber style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={8}><Form.Item name="smtp_security" label={t('p3.security')}><Select options={['starttls', 'ssl', 'none'].map((x) => ({ value: x, label: x.toUpperCase() }))} /></Form.Item></Col>
            <Col span={12}><Form.Item name="smtp_username" label={t('p3.username')}><Input /></Form.Item></Col>
            <Col span={12}><Form.Item name="smtp_password" label={t('p3.password')}><Input.Password /></Form.Item></Col>
            <Col span={12}><Form.Item name="imap_host" label={t('p3.imapHost')}><Input /></Form.Item></Col>
            <Col span={6}><Form.Item name="imap_port" label={t('p3.port')}><InputNumber style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="daily_cold_limit" label={t('p3.dailyLimit')}><InputNumber min={1} max={80} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={12}><Form.Item name="imap_username" label={`IMAP ${t('p3.username')}`}><Input /></Form.Item></Col>
            <Col span={12}><Form.Item name="imap_password" label={`IMAP ${t('p3.password')}`}><Input.Password /></Form.Item></Col>
            <Col span={24}><Form.Item name="signature" label={t('p3.signature')}><Input.TextArea rows={3} /></Form.Item></Col>
          </Row>
          <Button type="primary" htmlType="submit">{t('p3.bindAndTest')}</Button>
        </Form>
      </Modal>
      <Drawer open={!!hl} width={560} title={hl?.m.address} onClose={() => setHl(undefined)}>
        {hl && <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>{t('p3.score')} {hl.d.score} · {t(`p3.lv_${hl.d.level}`)} · {t('p3.sent7d')} {hl.d.sent} · {t('p3.bounce7d')} {(hl.d.hard_bounce_rate * 100).toFixed(2)}% · {t('p3.complaint7d')} {(hl.d.complaint_rate * 100).toFixed(3)}%</Typography.Text>
          <Table size="small" rowKey="created_at" pagination={false} dataSource={hl.d.events} columns={[
            { title: t('p3.level'), dataIndex: 'level', render: (v: string) => <Tag color={LEVEL[v] || 'red'}>{t(`p3.lv_${v}`, { defaultValue: String(v) })}</Tag> },
            { title: t('p3.metric'), dataIndex: 'metric', render: (v: string, r: any) => `${t(`p3.m_${v}`, { defaultValue: String(v) })} = ${v.endsWith('rate') ? (r.value * 100).toFixed(2) + '%' : r.value}` },
            { title: t('p3.cooldown'), dataIndex: 'cooldown_hours', render: (v: number) => v ? `${v}h` : '—' },
            { title: t('p3.time'), dataIndex: 'created_at', render: (v: string) => new Date(v).toLocaleString() },
          ]} />
        </Space>}
      </Drawer>
      <Drawer open={!!dom} width={640} title={dom?.domain} onClose={() => setDom(undefined)}>
        {dom && <Space direction="vertical" style={{ width: '100%' }}>
          {['spf', 'dkim', 'dmarc', 'mx'].map((k) => <div key={k}><b>{k.toUpperCase()}</b> {ok(dom.check_detail?.[k]?.status)} <Typography.Text type="secondary">{dom.check_detail?.[k]?.reason || dom.check_detail?.[k]?.record || ''} {dom.check_detail?.[k]?.advice || ''}</Typography.Text></div>)}
          <Typography.Title level={5}>{t('p3.toConfigure')}</Typography.Title>
          {(dom.check_detail?.recommended || []).map((r: any, i: number) => (
            <Card key={i} size="small"><div>{r.type} · <Typography.Text copyable>{r.host}</Typography.Text></div>
              <Typography.Text copyable code>{r.value}</Typography.Text><div><Typography.Text type="secondary">{r.note}</Typography.Text></div></Card>))}
          {!(dom.check_detail?.recommended || []).length && <Alert type="success" message={t('p3.dnsOk')} />}
        </Space>}
      </Drawer>
    </Space>
  )
}
