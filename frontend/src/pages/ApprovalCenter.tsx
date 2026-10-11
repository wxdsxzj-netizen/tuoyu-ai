/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd'
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
  const [busy, setBusy] = useState(false)
  const [failures, setFailures] = useState<string[]>([])
  const deciding = useRef(false)
  const load = () => {
    api.get('/approvals-center').then(r => setItems(r.data))
    api.get('/approval-requests').then(r => setMine(r.data))
    api.get('/approval-policies').then(r => setPols(r.data))
  }
  useEffect(load, [])
  const decide = (ids: string[], approve: boolean) => {
    if (deciding.current) return
    deciding.current = true; setBusy(true)
    let comment = ''
    Modal.confirm({
      title: approve ? t('b1.approve') : t('b1.reject'),
      content: <><div>{t('b1.confirmCount', { n: ids.length })}</div><Input.TextArea onChange={e => { comment = e.target.value }} placeholder={t('b1.comment')} /></>,
      onCancel: () => { deciding.current = false; setBusy(false) },
      onOk: async () => {
        const rows = items.filter(x => ids.includes(x.id))
        const failed = new Map<string, string>()
        const groups = new Map<string, any[]>()
        for (const row of rows) {
          const key = `${row.unified ? 'unified' : 'legacy'}:${row.type}`
          groups.set(key, [...(groups.get(key) || []), row])
        }
        try {
          for (const group of groups.values()) {
            // Each API accepts one approval type and at most 100 items.
            for (let offset = 0; offset < group.length; offset += 100) {
              const part = group.slice(offset, offset + 100)
              const requestIds = part.map(x => x.id)
              try {
                let results: Record<string, string>
                if (part[0].unified && part.length === 1) {
                  const r = await api.post(`/approval-requests/${requestIds[0]}:decide`, { approve, comment })
                  results = { [requestIds[0]]: r.data.status }
                } else {
                  const path = part[0].unified ? '/approval-requests:batch-decide' : '/approvals-center/legacy:batch-decide'
                  const r = await api.post(path, { ids: requestIds, approve, comment, confirm_send: true,
                    ...(!part[0].unified ? { type: part[0].type } : {}) })
                  results = r.data
                }
                for (const row of part) {
                  const status = results[row.id]
                  const accepted = row.unified || ['content', 'quote'].includes(row.type) ? [approve ? 'approved' : 'rejected']
                    : row.type === 'agent' ? ['queued', 'running', 'completed', 'waiting_approval', 'paused']
                      : row.type === 'inquiry_review' ? (approve ? ['assigned', 'pending_assign'] : ['closed']) : []
                  if (!accepted.includes(status)) failed.set(row.id, `${row.title}：${status || '未返回处理结果'}`)
                }
              } catch (error: any) {
                for (const row of part) failed.set(row.id, `${row.title}：${error.response?.data?.error?.code || '结果待确认，请刷新核查后重试'}`)
              }
            }
          }
          setFailures([...failed.values()]); setSel([...failed.keys()]); load()
          if (failed.size) message.warning(t('b3.batchPartial', { n: failed.size }))
          else message.success(t('b1.done'))
        } finally { deciding.current = false; setBusy(false) }
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
        <Button disabled={busy || !sel.length} type="primary" onClick={() => decide(sel, true)}>{t('b1.batchApprove')}</Button>
        <Button disabled={busy || !sel.length} danger onClick={() => decide(sel, false)}>{t('b1.batchReject')}</Button>
      </Space>
      <Alert type="warning" showIcon style={{ marginBottom: 8, display: failures.length ? undefined : 'none' }} message="部分审批未完成" description={failures.join('；')} />
      <Table rowKey="id" size="small" dataSource={items} pagination={{ pageSize: 20 }}
        rowSelection={{ getCheckboxProps: () => ({ disabled: busy }), selectedRowKeys: sel, onChange: k => setSel(k as string[]), }}
        columns={[
          { title: t('b1.type'), dataIndex: 'label', render: (v, r: any) => <Tag color={r.overdue ? 'red' : 'blue'}>{v}</Tag> },
          { title: t('b1.title'), dataIndex: 'title' },
          { title: t('b1.dueAt'), dataIndex: 'due_at', render: (v, r: any) => v ? <span style={{ color: r.overdue ? 'red' : undefined }}>{new Date(v).toLocaleString()}</span> : '—' },
          { title: t('b1.status'), dataIndex: 'status', render: v => v === 'escalated' ? <Tag color="orange">{t('b1.escalated')}</Tag> : null },
          { title: '', render: (_, r: any) => r.unified ? <Space>
            <Button disabled={busy} size="small" type="primary" onClick={() => decide([r.id], true)}>{t('b1.approve')}</Button>
            <Button disabled={busy} size="small" danger onClick={() => decide([r.id], false)}>{t('b1.reject')}</Button></Space>
            : <Space>
            <Button disabled={busy} size="small" type="primary" onClick={() => decide([r.id], true)}>{t('b1.approve')}</Button>
            <Button disabled={busy} size="small" danger onClick={() => decide([r.id], false)}>{t('b1.reject')}</Button>
            <Button size="small" onClick={() => nav(r.link)}>{t('b1.goHandle')}</Button></Space> },
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
