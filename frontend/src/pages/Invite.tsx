/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, Result, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { api } from '../api/client'

export default function InvitePage() {
  const { t } = useTranslation()
  const { token } = useParams()
  const [inv, setInv] = useState<any>()
  const [err, setErr] = useState(false)
  const [done, setDone] = useState(false)
  useEffect(() => { api.get(`/public/invitations/${token}`).then(r => setInv(r.data)).catch(() => setErr(true)) }, [token])
  const submit = async (v: any) => { await api.post(`/public/invitations/${token}:accept`, v); setDone(true) }
  return <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f0f4fb' }}>
    <Card style={{ width: 420 }}>
      {err ? <Result status="warning" title={t('b1.inviteInvalid')} /> : done ? <Result status="success" title={t('b1.inviteDone')} extra={<Button type="primary" href="/login">{t('login.submit')}</Button>} /> : inv && <>
        <Typography.Title level={4}>{t('b1.inviteTitle', { tenant: inv.tenant })}</Typography.Title>
        <Alert type="info" showIcon message={`${inv.email} · ${t('b1.role_' + inv.role_code)}`} style={{ marginBottom: 12 }} />
        <Form layout="vertical" onFinish={submit}>
          <Form.Item name="name" label={t('b1.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="password" label={t('login.password')} extra={t('b1.pwRule')} rules={[{ required: true, min: 10 }]}><Input.Password /></Form.Item>
          <Button type="primary" htmlType="submit" block>{t('b1.acceptInvite')}</Button>
        </Form>
      </>}
    </Card>
  </div>
}
