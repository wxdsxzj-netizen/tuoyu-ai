import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Col, Empty, Form, Input, Modal, Row, Select, Space, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useAuth } from '../auth'

interface Icp { id: string; name: string; description: string; industries: string[]; countries: string[]; employee_ranges: string[]; roles: string[]; buying_traits: string[]; exclusions: string[]; status: string; source: string; sample_count: number; version: number }
const LIST_FIELDS = ['industries', 'countries', 'employee_ranges', 'roles', 'buying_traits', 'exclusions'] as const
const LABEL: Record<string, string> = { industries: 'icp.industries', countries: 'icp.countries', employee_ranges: 'icp.employeeRanges', roles: 'icp.roles', buying_traits: 'icp.traits', exclusions: 'icp.exclusions' }

export default function IcpPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const manage = can('icp.manage')
  const [rows, setRows] = useState<Icp[]>([])
  const [editing, setEditing] = useState<Icp | 'new' | null>(null)
  const [form] = Form.useForm()
  const load = useCallback(async () => setRows((await api.get<Icp[]>('/icps')).data), [])
  useEffect(() => { load() }, [load])
  const generate = async () => { const r = await api.post('/icps:generate'); message.success(`+${r.data.length}`); load() }
  const act = async (i: Icp, a: string) => { await api.post(`/icps/${i.id}:${a}`); load() }
  const open = (i: Icp | 'new') => {
    form.resetFields()
    form.setFieldsValue(i === 'new' ? { name: '', description: '', ...Object.fromEntries(LIST_FIELDS.map(f => [f, []])) } : i)
    setEditing(i)
  }
  const save = async () => {
    const v = await form.validateFields()
    if (editing === 'new') await api.post('/icps', v)
    else if (editing) await api.patch(`/icps/${editing.id}`, { ...v, version: editing.version })
    setEditing(null); load()
  }
  const color: Record<string, string> = { draft: 'default', active: 'green', disabled: 'red' }
  return (
    <Card title={t('menu.icp')} extra={manage && <Space>
      <Typography.Text type="secondary">{t('icp.hint')}</Typography.Text>
      <Button onClick={generate}>{t('icp.generate')}</Button>
      <Button type="primary" onClick={() => open('new')}>{t('icp.create')}</Button></Space>}>
      {!rows.length && <Empty />}
      <Row gutter={[16, 16]}>
        {rows.map((i) => (
          <Col xs={24} md={12} xl={8} key={i.id}>
            <Card size="small" title={<Space>{i.name}<Tag color={color[i.status]}>{t(`status.${i.status}`)}</Tag></Space>}
              extra={manage && <Space>
                <a onClick={() => open(i)}>{t('icp.edit')}</a>
                {i.status === 'active' ? <a onClick={() => act(i, 'deactivate')}>{t('icp.deactivate')}</a> : <a onClick={() => act(i, 'activate')}>{t('icp.activate')}</a>}
              </Space>}>
              <Typography.Paragraph>{i.description}</Typography.Paragraph>
              {LIST_FIELDS.map((f) => i[f].length ? <div key={f} style={{ marginBottom: 4 }}><Typography.Text type="secondary">{t(LABEL[f])}：</Typography.Text>{i[f].map((x) => <Tag key={x}>{x}</Tag>)}</div> : null)}
              {i.sample_count > 0 && <Typography.Text type="secondary">{t('icp.samples')}：{i.sample_count}</Typography.Text>}
            </Card>
          </Col>
        ))}
      </Row>
      <Modal open={!!editing} title={editing === 'new' ? t('icp.create') : t('icp.edit')} onOk={save} onCancel={() => setEditing(null)} destroyOnClose width={640}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('icp.name')} rules={[{ required: true, max: 64 }]}><Input /></Form.Item>
          <Form.Item name="description" label={t('icp.description')}><Input.TextArea rows={2} /></Form.Item>
          {LIST_FIELDS.map((f) => <Form.Item key={f} name={f} label={t(LABEL[f])}><Select mode="tags" tokenSeparators={[',', '，']} /></Form.Item>)}
        </Form>
      </Modal>
    </Card>
  )
}
