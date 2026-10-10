import { useState } from 'react'
import { Button, Card, Form, Input, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

export default function LoginPage() {
  const { t } = useTranslation()
  const { setToken } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [busy, setBusy] = useState(false)
  const onFinish = async (v: Record<string, string>) => {
    setBusy(true)
    try {
      const r = await api.post(mode === 'login' ? '/auth/login' : '/auth/register', v)
      await setToken(r.data.access_token)
    } catch { /* 已统一提示 */ } finally { setBusy(false) }
  }
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f0f4fb' }}>
      <Card style={{ width: 380 }}>
        <Typography.Title level={3} style={{ textAlign: 'center' }}>{t('login.title')}</Typography.Title>
        <Form layout="vertical" onFinish={onFinish} key={mode}>
          {mode === 'register' && <>
            <Form.Item name="tenant_name" label={t('login.tenantName')} rules={[{ required: true, message: t('common.required') }]}><Input /></Form.Item>
            <Form.Item name="name" label={t('login.name')} rules={[{ required: true, message: t('common.required') }]}><Input /></Form.Item>
          </>}
          <Form.Item name="email" label={t('login.email')} rules={[{ required: true, type: 'email' }]}><Input autoComplete="username" /></Form.Item>
          <Form.Item name="password" label={t('login.password')} rules={[{ required: true, min: mode === 'register' ? 8 : 1 }]}><Input.Password autoComplete="current-password" /></Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>{mode === 'login' ? t('login.submit') : t('login.register')}</Button>
        </Form>
        <Button type="link" block onClick={() => setMode(mode === 'login' ? 'register' : 'login')}>
          {mode === 'login' ? t('login.toRegister') : t('login.toLogin')}
        </Button>
      </Card>
    </div>
  )
}
