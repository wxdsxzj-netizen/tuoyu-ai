import { Card, Descriptions } from 'antd'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../auth'

export default function Dashboard() {
  const { me } = useAuth()
  const { t } = useTranslation()
  return (
    <Card title={t('common.welcome', { name: me?.user.name })}>
      <Descriptions column={1}>
        <Descriptions.Item label={t('tenant.name')}>{me?.tenant.name}</Descriptions.Item>
        <Descriptions.Item label={t('users.role')}>{me?.user.role_name}</Descriptions.Item>
        <Descriptions.Item label={t('users.email')}>{me?.user.email}</Descriptions.Item>
      </Descriptions>
    </Card>
  )
}
