/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { Alert, Card, Tag, Typography } from 'antd'
import { useParams } from 'react-router-dom'
import axios from 'axios'

export default function SharedReportPage() {
  const { token } = useParams()
  const [r, setR] = useState<any>()
  const [err, setErr] = useState(false)
  useEffect(() => { axios.get(`/api/v1/public/research/${token}`).then(x => setR(x.data)).catch(() => setErr(true)) }, [token])
  if (err) return <Alert type="error" showIcon message="链接已失效或已撤销" style={{ margin: 40 }} />
  if (!r) return null
  return <Card style={{ maxWidth: 900, margin: '24px auto' }} title={<>{r.company} <Tag>{r.domain}</Tag><Tag>v{r.version}</Tag></>}
    extra={<span>{r.tenant} · 有效至 {new Date(r.expires_at).toLocaleDateString()}</span>}>
    {r.sections.map((s: any) => <div key={s.key} style={{ marginBottom: 12 }}>
      <Typography.Title level={5}>{s.title}</Typography.Title>
      {s.items.length ? <ul>{s.items.map((it: any, i: number) => <li key={i}>{it.text}{it.inferred && <Tag>推断</Tag>}{(it.evidence || []).map((e: string) => <Tag key={e} color="blue">{e}</Tag>)}</li>)}</ul>
        : <Typography.Text type="secondary">未找到相关信息</Typography.Text>}
    </div>)}
    <Typography.Title level={5}>证据来源</Typography.Title>
    <ul>{r.evidence.map((e: any) => <li key={e.id}><Tag color="blue">{e.id}</Tag>{e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}</li>)}</ul>
  </Card>
}
