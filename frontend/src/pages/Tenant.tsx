import { useEffect } from 'react'
import { Button, Card, Form, Input, Select, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

export default function TenantPage() {
  const { t } = useTranslation()
  const { me, can, setToken } = useAuth()
  const [form] = Form.useForm()
  useEffect(() => { if (me) form.setFieldsValue(me.tenant) }, [me, form])
  const save = async (v: Record<string, string>) => {
    await api.patch('/tenant', { name: v.name, locale: v.locale, timezone: v.timezone })
    message.success(t('common.saved'))
    await setToken(localStorage.getItem('tl_token') || '')
  }
  const editable = can('tenant.update')
  return (
    <Card title={t('menu.tenant')} style={{ maxWidth: 560 }}>
      <Form form={form} layout="vertical" onFinish={save} disabled={!editable}>
        <Form.Item name="name" label={t('tenant.name')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="slug" label={t('tenant.slug')}><Input disabled /></Form.Item>
        <Form.Item name="locale" label={t('tenant.locale')}><Select options={[{ value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }]} /></Form.Item>
        <Form.Item name="timezone" label={t('tenant.timezone')}><Input /></Form.Item>
        {editable && <Button type="primary" htmlType="submit">{t('tenant.save')}</Button>}
      </Form>
    </Card>
  )
}
