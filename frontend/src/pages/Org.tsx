/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, DatePicker, Form, Input, Modal, Row, Select, Space, Table, Tabs, Tag, Tree, Typography, message } from 'antd'
import dayjs from 'dayjs'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

const flat = (nodes: any[], out: any[] = [], depth = 0): any[] => { nodes.forEach(n => { out.push({ ...n, depth }); flat(n.children, out, depth + 1) }); return out }

export default function OrgPage() {
  const { t } = useTranslation()
  const { can, me } = useAuth()
  const [tree, setTree] = useState<any[]>([])
  const [members, setMembers] = useState<any[]>([])
  const [invs, setInvs] = useState<any[]>([])
  const [edit, setEdit] = useState<any>()
  const [off, setOff] = useState<any>()
  const [link, setLink] = useState<string>()
  const manage = can('org.manage')
  const load = () => {
    api.get('/departments').then(r => setTree(r.data))
    api.get('/org/members').then(r => setMembers(r.data))
    if (manage) api.get('/invitations').then(r => setInvs(r.data))
  }
  useEffect(load, [])
  const depts = flat(tree)
  const dname = (id?: string) => depts.find(d => d.id === id)?.name || '—'
  const uname = (id?: string) => members.find(m => m.id === id)?.name || '—'
  const toTree = (ns: any[]): any[] => ns.map(n => ({ key: n.id, title: <Space>{n.name}<Tag>{n.members}</Tag>{n.manager_id && <Typography.Text type="secondary">{t('b1.manager')}: {uname(n.manager_id)}</Typography.Text>}
    {manage && <a onClick={() => api.delete(`/departments/${n.id}`).then(load)}>{t('b1.delete')}</a>}</Space>, children: toTree(n.children) }))
  const addDept = async (v: any) => { await api.post('/departments', v); message.success(t('b1.saved')); load() }
  const saveEdit = async (v: any) => {
    const [vf, vt] = v.vacation || []
    await api.put(`/users/${edit.id}/org`, { department_id: v.department_id || null, timezone: v.timezone || null, work_start: v.work_start, work_end: v.work_end,
      vacation_from: vf ? vf.format('YYYY-MM-DD') : null, vacation_to: vt ? vt.format('YYYY-MM-DD') : null, delegate_id: v.delegate_id || null })
    setEdit(undefined); load()
  }
  const act = async (u: any, a: string) => { await api.post(`/users/${u.id}:${a}`); message.success(t('b1.done')); load() }
  const offboard = async (v: any) => {
    const r = await api.post(`/users/${off.u.id}:offboard`, { target_id: v.target_id })
    Modal.success({ title: t('b1.offboardDone'), content: Object.entries(r.data.transferred).filter(([, n]) => n).map(([k, n]) => `${t('b1.obj_' + k, { defaultValue: k })} ${n}`).join('，') || '—' })
    setOff(undefined); load()
  }
  const startOff = async (u: any) => { const r = await api.post(`/users/${u.id}:handover-preview`); setOff({ u, preview: r.data }) }
  const invite = async (v: any) => { const r = await api.post('/invitations', v); setLink(r.data.invite_url); load() }
  const active = members.filter(m => m.status === 'active')
  return <><Tabs items={[
    { key: 'm', label: t('b1.members'), children: <Table rowKey="id" size="small" dataSource={members} columns={[
      { title: t('b1.name'), dataIndex: 'name', render: (v, r: any) => <>{v} <Typography.Text type="secondary">{r.email}</Typography.Text></> },
      { title: t('b1.role'), dataIndex: 'role', render: v => t('b1.role_' + v) },
      { title: t('b1.department'), dataIndex: 'department_id', render: dname },
      { title: t('b1.status'), dataIndex: 'status', render: (v, r: any) => <Space size={2}><Tag color={v === 'active' ? 'green' : 'default'}>{t('b1.us_' + v)}</Tag>
        {r.on_vacation && <Tag color="orange">{t('b1.onVacation')}</Tag>}{r.locked && <Tag color="red">{t('b1.locked')}</Tag>}{r.totp_enabled && <Tag color="blue">2FA</Tag>}</Space> },
      { title: t('b1.delegate'), dataIndex: 'delegate_id', render: uname },
      { title: '', render: (_, r: any) => <Space size={4} wrap>
        {manage && <a onClick={() => setEdit(r)}>{t('b1.edit')}</a>}
        {can('security.manage') && r.locked && <a onClick={() => act(r, 'unlock')}>{t('b1.unlock')}</a>}
        {can('security.manage') && <a onClick={() => act(r, 'revoke-sessions')}>{t('b1.revokeSessions')}</a>}
        {manage && r.status === 'active' && r.role !== 'owner' && r.id !== me?.user.id && <a style={{ color: 'red' }} onClick={() => startOff(r)}>{t('b1.offboard')}</a>}
      </Space> },
    ]} /> },
    { key: 'd', label: t('b1.departments'), children: <Row gutter={16}>
      <Col span={14}><Card size="small">{tree.length ? <Tree defaultExpandAll treeData={toTree(tree)} selectable={false} /> : <Typography.Text type="secondary">{t('b1.noDept')}</Typography.Text>}</Card></Col>
      {manage && <Col span={10}><Card size="small" title={t('b1.newDept')}><Form layout="vertical" onFinish={addDept}>
        <Form.Item name="name" label={t('b1.name')} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="parent_id" label={t('b1.parentDept')}><Select allowClear options={depts.map(d => ({ value: d.id, label: '　'.repeat(d.depth) + d.name }))} /></Form.Item>
        <Form.Item name="manager_id" label={t('b1.manager')}><Select allowClear options={active.map(m => ({ value: m.id, label: m.name }))} /></Form.Item>
        <Button type="primary" htmlType="submit">{t('b1.save')}</Button><Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>{t('b1.deptHint')}</Typography.Paragraph>
      </Form></Card></Col>}
    </Row> },
    manage && { key: 'i', label: t('b1.invitations'), children: <>
      <Card size="small" style={{ marginBottom: 12 }}><Form layout="inline" onFinish={invite}>
        <Form.Item name="email" rules={[{ required: true, type: 'email' }]}><Input placeholder={t('login.email')} /></Form.Item>
        <Form.Item name="role_code" initialValue="sales"><Select style={{ width: 120 }} options={['sales', 'admin'].map(v => ({ value: v, label: t('b1.role_' + v) }))} /></Form.Item>
        <Form.Item name="department_id"><Select allowClear placeholder={t('b1.department')} style={{ width: 160 }} options={depts.map(d => ({ value: d.id, label: d.name }))} /></Form.Item>
        <Button type="primary" htmlType="submit">{t('b1.invite')}</Button>
      </Form>{link && <Alert style={{ marginTop: 8 }} type="success" message={t('b1.inviteLink')} description={<Typography.Text copyable>{link}</Typography.Text>} />}</Card>
      <Table rowKey="id" size="small" dataSource={invs} columns={[
        { title: t('login.email'), dataIndex: 'email' }, { title: t('b1.role'), dataIndex: 'role_code', render: v => t('b1.role_' + v) },
        { title: t('b1.status'), dataIndex: 'status', render: v => <Tag>{t('b1.is_' + v)}</Tag> },
        { title: t('b1.expiresAt'), dataIndex: 'expires_at', render: v => new Date(v).toLocaleString() },
        { title: '', render: (_, r: any) => r.status === 'pending' && <a onClick={() => api.post(`/invitations/${r.id}:revoke`).then(load)}>{t('b1.revoke')}</a> },
      ]} /></> },
  ].filter(Boolean) as any} />
    <OrgModals {...{ t, edit, setEdit, saveEdit, depts, active, off, setOff, offboard }} />
  </>
}

function OrgModals({ t, edit, setEdit, saveEdit, depts, active, off, setOff, offboard }: any) {
  return <>
    <Modal open={!!edit} title={edit?.name} footer={null} onCancel={() => setEdit(undefined)} destroyOnClose>
      {edit && <Form layout="vertical" onFinish={saveEdit} initialValues={{ department_id: edit.department_id, timezone: edit.timezone, work_start: edit.work_start, work_end: edit.work_end,
        delegate_id: edit.delegate_id, vacation: edit.vacation_from ? [dayjs(edit.vacation_from), dayjs(edit.vacation_to)] : undefined }}>
        <Form.Item name="department_id" label={t('b1.department')}><Select allowClear options={depts.map((d: any) => ({ value: d.id, label: d.name }))} /></Form.Item>
        <Form.Item name="timezone" label={t('b1.timezone')}><Input placeholder="Asia/Shanghai" /></Form.Item>
        <Space><Form.Item name="work_start" label={t('b1.workStart')}><Input style={{ width: 90 }} /></Form.Item><Form.Item name="work_end" label={t('b1.workEnd')}><Input style={{ width: 90 }} /></Form.Item></Space>
        <Form.Item name="vacation" label={t('b1.vacation')}><DatePicker.RangePicker /></Form.Item>
        <Form.Item name="delegate_id" label={t('b1.delegate')} extra={t('b1.delegateHint')}><Select allowClear options={active.filter((m: any) => m.id !== edit.id).map((m: any) => ({ value: m.id, label: m.name }))} /></Form.Item>
        <Button type="primary" htmlType="submit" block>{t('b1.save')}</Button>
      </Form>}
    </Modal>
    <Modal open={!!off} title={t('b1.offboardTitle', { name: off?.u.name })} footer={null} onCancel={() => setOff(undefined)} destroyOnClose>
      {off && <><Alert type="warning" showIcon message={t('b1.offboardWarn')} style={{ marginBottom: 8 }}
        description={Object.entries(off.preview).filter(([, n]) => n).map(([k, n]) => `${t('b1.obj_' + k, { defaultValue: k })} ${n}`).join('，') || t('b1.nothing')} />
        <Form layout="vertical" onFinish={offboard}>
          <Form.Item name="target_id" label={t('b1.handoverTo')} rules={[{ required: true }]}><Select options={active.filter((m: any) => m.id !== off.u.id).map((m: any) => ({ value: m.id, label: m.name }))} /></Form.Item>
          <Button danger type="primary" htmlType="submit" block>{t('b1.offboardConfirm')}</Button>
        </Form></>}
    </Modal>
  </>
}
