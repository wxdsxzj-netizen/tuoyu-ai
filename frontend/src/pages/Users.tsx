import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import dayjs from 'dayjs'
import { api, type UserRow } from '../api/client'
import { useAuth } from '../auth'

export default function UsersPage() {
  const { t } = useTranslation()
  const { can, me } = useAuth()
  const manage = can('user.manage')
  const [rows, setRows] = useState<UserRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<UserRow | 'new' | null>(null)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.get('/users', { params: { page, size: 20, q: q || undefined } })
      setRows(r.data.items); setTotal(r.data.total)
    } finally { setLoading(false) }
  }, [page, q])
  useEffect(() => { load() }, [load])

  const roleOpts = [{ value: 'admin', label: '管理员' }, { value: 'sales', label: '业务员' }]
  const submit = async () => {
    const v = await form.validateFields()
    if (editing === 'new') await api.post('/users', v)
    else if (editing) await api.patch(`/users/${editing.id}`, { ...v, password: v.password || undefined, version: editing.version })
    message.success(t('common.saved')); setEditing(null); load()
  }
  const open = (u: UserRow | 'new') => {
    setEditing(u)
    form.setFieldsValue(u === 'new' ? { role_code: 'sales', email: '', name: '', password: '' } : { ...u, password: '' })
  }

  return (
    <Card title={t('menu.users')} extra={<Space>
      <Input.Search placeholder={t('users.search')} allowClear onSearch={(v) => { setPage(1); setQ(v) }} />
      {manage && <Button type="primary" onClick={() => open('new')}>{t('users.add')}</Button>}
    </Space>}>
      <Table rowKey="id" loading={loading} dataSource={rows}
        pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
        columns={[
          { title: t('users.name'), dataIndex: 'name' },
          { title: t('users.email'), dataIndex: 'email' },
          { title: t('users.role'), dataIndex: 'role_name', render: (v, r) => <Tag color={r.role_code === 'owner' ? 'gold' : r.role_code === 'admin' ? 'blue' : 'default'}>{v}</Tag> },
          { title: t('users.status'), dataIndex: 'is_active', render: (v) => v ? <Tag color="green">{t('users.active')}</Tag> : <Tag>{t('users.disabled')}</Tag> },
          { title: t('users.lastLogin'), dataIndex: 'last_login_at', render: (v) => v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '-' },
          ...(manage ? [{ title: t('users.actions'), key: 'a', render: (_: unknown, r: UserRow) => r.role_code === 'owner' || r.id === me?.user.id ? null : (
            <Space>
              <a onClick={() => open(r)}>{t('users.edit')}</a>
              <Popconfirm title={t('users.confirmDelete')} onConfirm={async () => { await api.delete(`/users/${r.id}`); load() }}>
                <a style={{ color: '#cf1322' }}>{t('users.delete')}</a>
              </Popconfirm>
            </Space>) }] : []),
        ]} />
      <Modal open={!!editing} title={editing === 'new' ? t('users.add') : t('users.edit')} onOk={submit} onCancel={() => setEditing(null)} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="email" label={t('users.email')} rules={[{ required: true, type: 'email' }]}><Input disabled={editing !== 'new'} /></Form.Item>
          <Form.Item name="name" label={t('users.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="role_code" label={t('users.role')} rules={[{ required: true }]}><Select options={roleOpts} /></Form.Item>
          <Form.Item name="password" label={editing === 'new' ? t('users.password') : t('users.newPassword')}
            rules={editing === 'new' ? [{ required: true, min: 8 }] : [{ min: 8 }]}><Input.Password /></Form.Item>
          {editing !== 'new' && <Form.Item name="is_active" label={t('users.status')} valuePropName="checked"><Switch /></Form.Item>}
        </Form>
      </Modal>
    </Card>
  )
}
