/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Card, List, Tag, Typography } from 'antd'
import axios from 'axios'

const C: Record<string, string> = { operational: 'green', degraded: 'gold', partial_outage: 'orange', major_outage: 'red' }
const ZH: Record<string, string> = { operational: '正常', degraded: '性能下降', partial_outage: '部分故障', major_outage: '严重故障',
  web: '网页', api: '接口', sending: '发信', inbox_sync: '收件同步', ai: 'AI 服务', data_hub: '数据中枢' }

export default function StatusPage() {
  const [s, setS] = useState<any>()
  useEffect(() => { const f = () => axios.get('/api/v1/public/status').then(r => setS(r.data)); f(); const i = setInterval(f, 60000); return () => clearInterval(i) }, [])
  if (!s) return null
  return <Card style={{ maxWidth: 820, margin: '24px auto' }} title={<>服务状态 / Service status <Tag color={C[s.overall]}>{ZH[s.overall]} · {s.overall}</Tag></>}
    extra={<span>更新于 {new Date(s.updated_at).toLocaleString()}</span>}>
    <List size="small" dataSource={Object.entries(s.components)} renderItem={([k, v]: any) => <List.Item>{ZH[k] || k}<Tag color={C[v]}>{ZH[v]}</Tag></List.Item>} />
    <Typography.Title level={5} style={{ marginTop: 16 }}>近 90 天事件</Typography.Title>
    {s.incidents.length ? s.incidents.map((i: any) => <Card key={i.detected_at} size="small" style={{ marginBottom: 8 }} title={<>{i.title} <Tag>{i.severity}</Tag><Tag color={i.resolved_at ? 'green' : 'red'}>{i.status}</Tag></>}>
      {i.updates.map((u: any) => <div key={u.at}><Typography.Text type="secondary">{new Date(u.at).toLocaleString()}</Typography.Text> [{u.status}] {u.text}</div>)}</Card>)
      : <Typography.Text type="secondary">无事件</Typography.Text>}
  </Card>
}
