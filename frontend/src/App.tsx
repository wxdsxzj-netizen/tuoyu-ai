import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu, Dropdown, Spin, Space, Select, Typography } from 'antd'
import { TeamOutlined, SettingOutlined, AuditOutlined, DashboardOutlined, ThunderboltOutlined, UserOutlined, BookOutlined, AimOutlined, BankOutlined, ContactsOutlined, ImportOutlined, SafetyCertificateOutlined, MailOutlined, SendOutlined, CheckSquareOutlined, FileProtectOutlined, InboxOutlined, MessageOutlined, GiftOutlined, ShopOutlined, FireOutlined, RobotOutlined, BarChartOutlined, FileSearchOutlined, WalletOutlined, ScheduleOutlined, ApartmentOutlined, LockOutlined, RestOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { useAuth } from './auth'
import { safeReturnTo } from './authPaths'
import i18n, { LOCALE_KEY } from './i18n'
import LoginPage from './pages/Login'
import UsersPage from './pages/Users'
import TenantPage from './pages/Tenant'
import AuditPage from './pages/Audit'
import JobsPage from './pages/Jobs'
import Dashboard from './pages/Dashboard'
import KbPage from './pages/Kb'
import IcpPage from './pages/Icp'
import CompaniesPage from './pages/Companies'
import ContactsPage from './pages/Contacts'
import ImportsPage from './pages/Imports'
import CompliancePage from './pages/Compliance'
import DsrPage from './pages/Dsr'
import MailboxesPage from './pages/Mailboxes'
import MailboxOAuthCallback from './pages/MailboxOAuthCallback'
import SequencesPage from './pages/Sequences'
import ApprovalsPage from './pages/Approvals'
import DeliveryPage from './pages/Delivery'
import InboxPage from './pages/Inbox'
import HandoffsPage from './pages/Handoffs'
import CrmPage from './pages/Crm'
import ConfirmPage from './pages/Confirm'
import NotificationBell from './pages/Notifications'
import InquiriesPage from './pages/Inquiries'
import AgentPage from './pages/Agent'
import AnalyticsPage from './pages/Analytics'
import ResearchPage from './pages/Research'
import DiscoverPage from './pages/Discover'
import OutreachToolsPage from './pages/OutreachTools'
import InsightsPage from './pages/Insights'
import SharedReportPage from './pages/SharedReport'
import WidgetsPage from './pages/Widgets'
import SupportPage from './pages/Support'
import BillingPlusPage from './pages/BillingPlus'
import AiLabPage from './pages/AiLab'
import DataHubPage from './pages/DataHub'
import PlatformPage from './pages/Platform'
import WidgetEmbedPage from './pages/WidgetEmbed'
import StatusPage from './pages/Status'
import TodayPage from './pages/Today'
import ApprovalCenterPage from './pages/ApprovalCenter'
import OrgPage from './pages/Org'
import SecurityPage from './pages/Security'
import RecyclePage from './pages/Recycle'
import ProductsPage from './pages/Products'
import CrmPlusPage from './pages/CrmPlus'
import InvitePage from './pages/Invite'
import BillingPage from './pages/Billing'

function Shell() {
  const { me, can, logout } = useAuth()
  const { t } = useTranslation()
  const nav = useNavigate()
  const loc = useLocation()
  const items = [
    { key: '/', icon: <DashboardOutlined />, label: t('menu.dashboard') },
    { key: '/today', icon: <ScheduleOutlined />, label: t('menu.today') },
    { key: '/approvals-center', icon: <AuditOutlined />, label: t('menu.approvalCenter') },
    can('agent.run') && { key: '/agent', icon: <RobotOutlined />, label: t('menu.agent') },
    can('inbox.read') && { key: '/inbox', icon: <MessageOutlined />, label: t('menu.inbox') },
    can('inbox.read') && { key: '/inquiries', icon: <FireOutlined />, label: t('menu.inquiries') },
    can('handoff.accept') && { key: '/handoffs', icon: <GiftOutlined />, label: t('menu.handoffs') },
    can('crm.read') && { key: '/crm', icon: <ShopOutlined />, label: t('menu.crm') },
    can('company.read') && { key: '/discover', icon: <FileSearchOutlined />, label: t('menu.discover') },
    can('research.run') && { key: '/research', icon: <FileSearchOutlined />, label: t('menu.research') },
    can('sequence.read') && { key: '/outreach-tools', icon: <FileSearchOutlined />, label: t('menu.outreachTools') },
    can('inbox.read') && { key: '/widgets', icon: <MessageOutlined />, label: t('menu.widgets') },
    { key: '/ai-lab', icon: <BankOutlined />, label: t('menu.aiLab') },
    can('hub.read') && { key: '/data-hub', icon: <BankOutlined />, label: t('menu.dataHub') },
    { key: '/platform', icon: <BankOutlined />, label: t('menu.platform') },
    can('billing.read') && { key: '/billing-plus', icon: <BankOutlined />, label: t('menu.billingPlus') },
    { key: '/support', icon: <BookOutlined />, label: t('menu.support') },
    can('analytics.read') && { key: '/insights', icon: <FileSearchOutlined />, label: t('menu.insights') },
    can('analytics.read') && { key: '/analytics', icon: <BarChartOutlined />, label: t('menu.analytics') },
    can('kb.read') && { key: '/kb', icon: <BookOutlined />, label: t('menu.kb') },
    can('product.read') && { key: '/products', icon: <BookOutlined />, label: t('menu.products') },
    can('crm.read') && { key: '/crm-plus', icon: <BankOutlined />, label: t('menu.crmPlus') },
    can('icp.read') && { key: '/icp', icon: <AimOutlined />, label: t('menu.icp') },
    can('company.read') && { key: '/companies', icon: <BankOutlined />, label: t('menu.companies') },
    can('company.read') && { key: '/contacts', icon: <ContactsOutlined />, label: t('menu.contacts') },
    can('import.run') && { key: '/imports', icon: <ImportOutlined />, label: t('menu.imports') },
    can('sequence.read') && { key: '/sequences', icon: <SendOutlined />, label: t('menu.sequences') },
    can('sequence.read') && { key: '/approvals', icon: <CheckSquareOutlined />, label: t('menu.approvals') },
    can('sequence.read') && { key: '/delivery', icon: <InboxOutlined />, label: t('menu.delivery') },
    can('mailbox.read') && { key: '/mailboxes', icon: <MailOutlined />, label: t('menu.mailboxes') },
    can('compliance.read') && { key: '/compliance', icon: <SafetyCertificateOutlined />, label: t('menu.compliance') },
    can('dsr.manage') && { key: '/dsr', icon: <FileProtectOutlined />, label: t('menu.dsr') },
    can('user.read') && { key: '/users', icon: <TeamOutlined />, label: t('menu.users') },
    can('user.read') && { key: '/org', icon: <ApartmentOutlined />, label: t('menu.org') },
    { key: '/security', icon: <LockOutlined />, label: t('menu.security') },
    { key: '/recycle', icon: <RestOutlined />, label: t('menu.recycle') },
    can('tenant.read') && { key: '/tenant', icon: <SettingOutlined />, label: t('menu.tenant') },
    can('billing.read') && { key: '/billing', icon: <WalletOutlined />, label: t('menu.billing') },
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
          <NotificationBell />
          <Typography.Text type="secondary">{me?.tenant.name}</Typography.Text>
          <Dropdown menu={{ items: [{ key: 'logout', label: t('app.logout'), onClick: () => { logout(); nav('/login') } }] }}>
            <Space style={{ cursor: 'pointer' }}><UserOutlined />{me?.user.name}</Space>
          </Dropdown>
        </Layout.Header>
        <Layout.Content style={{ margin: 24 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/inquiries" element={<InquiriesPage />} />
            <Route path="/agent" element={<AgentPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/research" element={<ResearchPage />} />
            <Route path="/discover" element={<DiscoverPage />} />
            <Route path="/outreach-tools" element={<OutreachToolsPage />} />
            <Route path="/insights" element={<InsightsPage />} />
            <Route path="/widgets" element={<WidgetsPage />} />
            <Route path="/support" element={<SupportPage />} />
            <Route path="/billing-plus" element={<BillingPlusPage />} />
            <Route path="/ai-lab" element={<AiLabPage />} />
            <Route path="/data-hub" element={<DataHubPage />} />
            <Route path="/platform" element={<PlatformPage />} />
            <Route path="/billing" element={<BillingPage />} />
            <Route path="/today" element={<TodayPage />} />
            <Route path="/approvals-center" element={<ApprovalCenterPage />} />
            <Route path="/org" element={<OrgPage />} />
            <Route path="/security" element={<SecurityPage />} />
            <Route path="/recycle" element={<RecyclePage />} />
            <Route path="/products" element={<ProductsPage />} />
            <Route path="/crm-plus" element={<CrmPlusPage />} />
            <Route path="/handoffs" element={<HandoffsPage />} />
            <Route path="/crm" element={new URLSearchParams(loc.search).get('tab') === 'orders' ? <Navigate to="/crm-plus?tab=orders" replace /> : <CrmPage />} />
            <Route path="/kb" element={<KbPage />} />
            <Route path="/icp" element={<IcpPage />} />
            <Route path="/companies" element={<CompaniesPage />} />
            <Route path="/contacts" element={<ContactsPage />} />
            <Route path="/imports" element={<ImportsPage />} />
            <Route path="/compliance" element={<CompliancePage />} />
            <Route path="/dsr" element={<DsrPage />} />
            <Route path="/mailboxes" element={<MailboxesPage />} />
            <Route path="/mailboxes/oauth/:provider/callback" element={<MailboxOAuthCallback />} />
            <Route path="/sequences" element={<SequencesPage />} />
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/delivery" element={<DeliveryPage />} />
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
  const loc = useLocation()
  const destination = safeReturnTo(loc.state?.returnTo || new URLSearchParams(loc.search).get('returnTo'))
  return (
    <Routes>
      <Route path="/confirm/:token" element={<ConfirmPage />} />
      <Route path="/share/research/:token" element={<SharedReportPage />} />
      <Route path="/w/:key" element={<WidgetEmbedPage />} />
      <Route path="/status" element={<StatusPage />} />
      <Route path="/invite/:token" element={<InvitePage />} />
      <Route path="/login" element={loading ? <Spin fullscreen /> : me ? <Navigate to={destination} replace /> : <LoginPage />} />
      <Route path="/*" element={loading ? <Spin fullscreen /> : me ? <Shell /> : <Navigate to="/login" replace state={{ returnTo: loc.pathname + loc.search + loc.hash }} />} />
    </Routes>
  )
}
