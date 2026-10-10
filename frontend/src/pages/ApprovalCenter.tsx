/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Button, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth'

export default function ApprovalCenterPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const nav = useNavigate()
  const [items, setItems] = useState<any[]>([])
  const [mine, setMine] = useState<any[]>([])
  const [pols, setPols] = useState<any[]>([])
  const [sel, setSel] = useState<string[]>([])
  const load = () => {
    api.get('/approvals-center').then(r => setItems(r.data))
    api.get('/approval-requests').then(r => setMine(r.data))
    api.get('/approval-policies').then(r => setPols(r.data))
  }
  useEffect(load, [])
  const decide = (ids: string[], approve: boolean) => {
    let comment = ''
    Modal.confirm({
      title: approve ? t('b1.approve') : t('b1.reject'),
      content: <><div>{t('b1.confirmCount', { n: ids.length })}</div><Input.TextArea onChange={e => { comment = e.target.value }} placeholder={t('b1.comment')} /></>,
      onOk: async () => {
        if (ids.length === 1) await api.post(`/approval-requests/${ids[0]}:decide`, { approve, comment })
        else await api.post('/approval-requests:batch-decide', { ids, approve, comment, confirm_send: true })
        message.success(t('b1.done')); setSel([]); load()
      },
    })
  }
  const savePol = async (p: any) => {
    await api.put(`/approval-policies/${p.approval_type}`, { approver_roles: p.approver_roles, timeout_hours: p.timeout_hours, timeout_action: p.timeout_action, allow_self: p.allow_self })
    message.success(t('b1.saved')); load()
  }
  const upd = (i: number, k: string, v: any) => setPols(pols.map((p, j) => j === i ? { ...p, [k]: v } : p))
  return <Tabs items={[
    { key: 'todo', label: `${t('b1.waitingMe')} (${items.length})`, children: <>
      <Space style={{ marginBottom: 8 }}>
        <Button disabled={!sel.length} type="primary" onClick={() => decide(sel, true)}>{t('b1.batchApprove')}</Button>
        <Button disabled={!sel.length} danger onClick={() => decide(sel, false)}>{t('b1.batchReject')}</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={items} pagination={{ pageSize: 20 }}
        rowSelection={{ selectedRowKeys: sel, onChange: k => setSel(k as string[]), getCheckboxProps: (r: any) => ({ disabled: !r.unified }) }}
        columns={[
          { title: t('b1.type'), dataIndex: 'label', render: (v, r: any) => <Tag color={r.overdue ? 'red' : 'blue'}>{v}</Tag> },
          { title: t('b1.title'), dataIndex: 'title' },
          { title: t('b1.dueAt'), dataIndex: 'due_at', render: (v, r: any) => v ? <span style={{ color: r.overdue ? 'red' : undefined }}>{new Date(v).toLocaleString()}</span> : '—' },
          { title: t('b1.status'), dataIndex: 'status', render: v => v === 'escalated' ? <Tag color="orange">{t('b1.escalated')}</Tag> : null },
          { title: '', render: (_, r: any) => r.unified ? <Space>
            <Button size="small" type="primary" onClick={() => decide([r.id], true)}>{t('b1.approve')}</Button>
            <Button size="small" danger onClick={() => decide([r.id], false)}>{t('b1.reject')}</Button></Space>
            : <Button size="small" onClick={() => nav(r.link)}>{t('b1.goHandle')}</Button> },
        ]} />
    </> },
    { key: 'mine', label: t('b1.myRequests'), children: <Table rowKey="id" size="small" dataSource={mine} columns={[
      { title: t('b1.type'), dataIndex: 'label' }, { title: t('b1.title'), dataIndex: 'title' },
      { title: t('b1.status'), dataIndex: 'status', render: v => <Tag>{t('b1.st_' + v)}</Tag> }, { title: t('b1.comment'), dataIndex: 'comment' },
      { title: t('b1.createdAt'), dataIndex: 'created_at', render: v => new Date(v).toLocaleString() },
    ]} /> },
    { key: 'pol', label: t('b1.policies'), children: <Table rowKey="approval_type" size="small" dataSource={pols} pagination={false} columns={[
      { title: t('b1.type'), dataIndex: 'label' },
      { title: t('b1.approvers'), render: (_, p: any, i) => <Select mode="multiple" style={{ minWidth: 180 }} disabled={!can('org.manage')} value={p.approver_roles}
        onChange={v => upd(i, 'approver_roles', v)} options={['owner', 'admin', 'sales'].map(v => ({ value: v, label: t('b1.role_' + v) }))} /> },
      { title: t('b1.timeoutHours'), render: (_, p: any, i) => <InputNumber min={1} max={720} disabled={!can('org.manage')} value={p.timeout_hours} onChange={v => upd(i, 'timeout_hours', v)} /> },
      { title: t('b1.timeoutAction'), render: (_, p: any, i) => <Select style={{ width: 140 }} disabled={!can('org.manage')} value={p.timeout_action} onChange={v => upd(i, 'timeout_action', v)}
        options={['escalate', 'reject', 'approve'].filter(a => !(a === 'approve' && p.send_type)).map(v => ({ value: v, label: t('b1.ta_' + v) }))} /> },
      { title: t('b1.allowSelf'), render: (_, p: any, i) => <Switch disabled={!can('org.manage')} checked={p.allow_self} onChange={v => upd(i, 'allow_self', v)} /> },
      { title: '', render: (_, p: any) => can('org.manage') && <Button size="small" onClick={() => savePol(p)}>{t('b1.save')}</Button> },
    ]} /> },
  ]} />
}
