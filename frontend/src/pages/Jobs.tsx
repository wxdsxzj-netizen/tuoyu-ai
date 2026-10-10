import { useEffect, useRef, useState } from 'react'
import { Button, Card, Descriptions, Input, Progress, Space, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

interface Job { id: string; status: string; progress: number; result: Record<string, unknown> | null; error: string | null }

export default function JobsPage() {
  const { t } = useTranslation()
  const [job, setJob] = useState<Job | null>(null)
  const [prompt, setPrompt] = useState('用一句话介绍拓域 AI')
  const [reply, setReply] = useState('')
  const timer = useRef<number>()
  useEffect(() => () => window.clearInterval(timer.current), [])
  const run = async () => {
    const r = await api.post<Job>('/jobs/example', { steps: 3 })
    setJob(r.data)
    window.clearInterval(timer.current)
    timer.current = window.setInterval(async () => {
      const j = (await api.get<Job>(`/jobs/${r.data.id}`)).data
      setJob(j)
      if (['succeeded', 'failed'].includes(j.status)) window.clearInterval(timer.current)
    }, 1000)
  }
  const chat = async () => setReply((await api.post('/llm/chat', { prompt })).data.content)
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card title={t('jobs.run')} extra={<Button type="primary" onClick={run}>{t('jobs.run')}</Button>}>
        {job && <Descriptions column={1}>
          <Descriptions.Item label={t('jobs.status')}><Tag>{job.status}</Tag></Descriptions.Item>
          <Descriptions.Item label={t('jobs.progress')}><Progress percent={job.progress} style={{ width: 300 }} /></Descriptions.Item>
          <Descriptions.Item label={t('jobs.result')}><code>{JSON.stringify(job.result ?? job.error)}</code></Descriptions.Item>
        </Descriptions>}
      </Card>
      <Card title={t('jobs.chat')}>
        <Space.Compact style={{ width: '100%' }}>
          <Input value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <Button type="primary" onClick={chat}>{t('jobs.send')}</Button>
        </Space.Compact>
        {reply && <p style={{ marginTop: 12 }}>{reply}</p>}
      </Card>
    </Space>
  )
}
