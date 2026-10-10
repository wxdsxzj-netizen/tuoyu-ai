import { useState } from 'react'
import { Button, Card, Form, Input, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

export default function LoginPage() {
  const { t } = useTranslation()
  const { setToken } = useAuth()
  const [busy, setBusy] = useState(false)
  const [mfa, setMfa] = useState<string>()
  const onFinish = async (v: Record<string, string>) => {
    setBusy(true)
    try {
      const r = mfa ? await api.post('/auth/mfa', { mfa_token: mfa, code: v.code }) : await api.post('/auth/login', v)
      if (r.data.mfa_required) { setMfa(r.data.mfa_token); return }
      await setToken(r.data.access_token)
    } catch { /* 已统一提示 */ } finally { setBusy(false) }
  }
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f0f4fb' }}>
      <Card style={{ width: 380 }}>
        <Typography.Title level={3} style={{ textAlign: 'center' }}>{t('login.title')}</Typography.Title>
        <Form layout="vertical" onFinish={onFinish}>
          {mfa ? <Form.Item name="code" label={t('b1.mfaCode')} rules={[{ required: true, len: 6 }]}><Input autoFocus inputMode="numeric" autoComplete="one-time-code" /></Form.Item> : <>
            <Form.Item name="email" label={t('login.email')} rules={[{ required: true, type: 'email' }]}><Input autoComplete="username" /></Form.Item>
            <Form.Item name="password" label={t('login.password')} rules={[{ required: true }]}><Input.Password autoComplete="current-password" /></Form.Item>
          </>}
          <Button type="primary" htmlType="submit" block loading={busy}>{t('login.submit')}</Button>
        </Form>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center', marginTop: 16, marginBottom: 0 }}>{t('login.noSignup')}</Typography.Paragraph>
      </Card>
    </div>
  )
}
