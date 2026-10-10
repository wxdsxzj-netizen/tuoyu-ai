/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Row, Statistic, Table, Tabs, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

const money = (m: number, c: string) => `${c} ${(m / 100).toFixed(2)}`

export default function BillingPage() {
  const { t } = useTranslation()
  const [s, setS] = useState<any>()
  const [led, setLed] = useState<any>({ items: [] })
  const [inv, setInv] = useState<any[]>([])
  useEffect(() => {
    api.get('/billing/summary').then(r => setS(r.data))
    api.get('/credits', { params: { page_size: 200 } }).then(r => setLed(r.data))
    api.get('/billing/invoices').then(r => setInv(r.data))
  }, [])
  const csv = async () => {
    const r = await api.get('/billing/ledger.csv', { responseType: 'blob' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(r.data); a.download = 'credit-ledger.csv'; a.click()
  }
  if (!s) return null
  return <>
    {s.low_balance && <Alert type="warning" showIcon message={t('p5.lowBalance')} style={{ marginBottom: 12 }} />}
    <Row gutter={16} style={{ marginBottom: 12 }}>
      <Col span={5}><Card size="small"><Statistic title={t('p5.balance')} value={s.balance} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.heldNow')} value={s.held} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.usedMonth')} value={s.used_this_month} /></Card></Col>
      <Col span={5}><Card size="small"><Statistic title={t('p5.granted')} value={s.granted_total} /></Card></Col>
      <Col span={4}><Card size="small"><Statistic title={t('p5.unpaid')} value={s.unpaid_invoices} /></Card></Col>
    </Row>
    <Typography.Paragraph type="secondary">{t('p5.topupHint')}</Typography.Paragraph>
    <Tabs items={[
      { key: 'l', label: t('p5.ledger'), children: <>
        <Button onClick={csv} style={{ marginBottom: 8 }}>{t('p5.exportCsv')}</Button>
        <Table rowKey="id" size="small" dataSource={led.items} columns={[
          { title: 'Time', dataIndex: 'at', render: v => new Date(v).toLocaleString() },
          { title: 'Type', dataIndex: 'entry_type', render: v => <Tag color={{ grant: 'green', correction: 'gold', hold: 'blue', release: 'cyan', consume: 'default' }[v as string]}>{v}</Tag> },
          { title: 'Action', dataIndex: 'action' }, { title: 'Qty', dataIndex: 'quantity' }, { title: 'Unit', dataIndex: 'unit_price' },
          { title: 'Δ', dataIndex: 'credits' }, { title: 'Result', dataIndex: 'result' }, { title: 'Note', dataIndex: 'note' }]} /></> },
      { key: 'i', label: t('p5.invoices'), children: <Table rowKey="id" size="small" dataSource={inv} columns={[
        { title: t('p5.invNo'), dataIndex: 'invoice_no' }, { title: t('p5.period'), dataIndex: 'period' }, { title: 'Desc', dataIndex: 'description' },
        { title: t('p5.amount'), render: (_, r: any) => money(r.amount_minor, r.currency) }, { title: t('p5.creditsCol'), dataIndex: 'credits' },
        { title: t('p5.status'), dataIndex: 'status', render: v => <Tag color={v === 'paid' ? 'green' : v === 'void' ? 'default' : 'orange'}>{v}</Tag> },
        { title: t('p5.method'), dataIndex: 'payment_method' }, { title: t('p5.paidAt'), dataIndex: 'paid_at', render: v => v ? new Date(v).toLocaleString() : '—' }]} /> },
      { key: 'p', label: t('p5.prices'), children: <Row gutter={16}>
        <Col span={12}><Descriptions size="small" bordered column={1} title={`v${s.price_version}`}>
          {Object.entries(s.prices).map(([k, v]) => <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>)}</Descriptions></Col>
        <Col span={12}><Descriptions size="small" bordered column={1} title={t('p5.byAction')}>
          {Object.entries(s.used_by_action).map(([k, v]) => <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>)}</Descriptions></Col>
      </Row> },
    ]} />
  </>
}
