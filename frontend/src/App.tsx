import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu, Dropdown, Spin, Space, Select, Typography } from 'antd'
import { TeamOutlined, SettingOutlined, AuditOutlined, DashboardOutlined, ThunderboltOutlined, UserOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { useAuth } from './auth'
import i18n, { LOCALE_KEY } from './i18n'
import LoginPage from './pages/Login'
import UsersPage from './pages/Users'
import TenantPage from './pages/Tenant'
import AuditPage from './pages/Audit'
import JobsPage from './pages/Jobs'
import Dashboard from './pages/Dashboard'

function Shell() {
  const { me, can, logout } = useAuth()
  const { t } = useTranslation()
  const nav = useNavigate()
  const loc = useLocation()
  const items = [
    { key: '/', icon: <DashboardOutlined />, label: t('menu.dashboard') },
    can('user.read') && { key: '/users', icon: <TeamOutlined />, label: t('menu.users') },
    can('tenant.read') && { key: '/tenant', icon: <SettingOutlined />, label: t('menu.tenant') },
    can('audit.read') && { key: '/audit', icon: <AuditOutlined />, label: t('menu.audit') },
    can('job.run') && { key: '/jobs', icon: <ThunderboltOutlined />, label: t('menu.jobs') },
  ].filter(Boolean) as { key: string; icon: JSX.Element; label: string }[]
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider theme="light" breakpoint="lg" collapsedWidth={0}>
        <div style={{ padding: 16, fontWeight: 700, fontSize: 18 }}>{t('app.name')}</div>
        <Menu mode="inline" selectedKeys={[loc.pathname]} items={items} onClick={(e) => nav(e.key)} />
      </Layout.Sider>
      <Layout>
        <Layout.Header style={{ background: '#fff', display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 16 }}>
          <Select size="small" value={i18n.language} style={{ width: 110 }}
            options={[{ value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }]}
            onChange={(v) => { localStorage.setItem(LOCALE_KEY, v); i18n.changeLanguage(v) }} />
          <Typography.Text type="secondary">{me?.tenant.name}</Typography.Text>
          <Dropdown menu={{ items: [{ key: 'logout', label: t('app.logout'), onClick: () => { logout(); nav('/login') } }] }}>
            <Space style={{ cursor: 'pointer' }}><UserOutlined />{me?.user.name}</Space>
          </Dropdown>
        </Layout.Header>
        <Layout.Content style={{ margin: 24 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/tenant" element={<TenantPage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </Layout.Content>
      </Layout>
    </Layout>
  )
}

export default function App() {
  const { me, loading } = useAuth()
  if (loading) return <Spin fullscreen />
  return (
    <Routes>
      <Route path="/login" element={me ? <Navigate to="/" /> : <LoginPage />} />
      <Route path="/*" element={me ? <Shell /> : <Navigate to="/login" />} />
    </Routes>
  )
}
