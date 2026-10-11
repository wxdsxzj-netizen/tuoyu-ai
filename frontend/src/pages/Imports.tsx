import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Radio, Select, Space, Table, Tag, Upload, message } from 'antd'
import type { UploadFile } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import dayjs from 'dayjs'
import { api, TOKEN_KEY } from '../api/client'
import { useSubmission } from '../api/useSubmission'
import { useJobPolling } from '../api/useJobPolling'
import { useAuth } from '../auth'

interface Job { id: string; type: string; filename: string; status: string; total: number; created: number; merged: number; skipped: number; failed: number; dry_run: boolean; created_at: string }
interface Preview { headers: string[]; sample: string[][]; total: number; mapping: Record<string, string>; missing_required: string[] }
interface FieldDef { field: string; required: boolean }
interface ImportIntent { type: string; mapping: Record<string, string>; dry_run: boolean; filename: string; file_hash: string }

export default function ImportsPage() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const submission = useSubmission<ImportIntent>(`import:${me?.tenant.id}:${me?.user.id}`)
  const [type, setType] = useState(submission.pending?.payload.type || 'companies')
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>(submission.pending?.payload.mapping || {})
  const [fields, setFields] = useState<Record<string, FieldDef[]>>({})
  const [dryRun, setDryRun] = useState(submission.pending?.payload.dry_run || false)
  const { items: jobs, reload: load, error: pollError } = useJobPolling<Job>('/imports')
  const [starting, setStarting] = useState(false)
  const startingRef = useRef(false)
  const previewVersion = useRef(0)

  useEffect(() => { api.get('/imports/fields').then((r) => setFields(r.data)) }, [])

  const doPreview = async (f: File, tp = type) => {
    const generation = ++previewVersion.current
    const fd = new FormData(); fd.append('type', tp); fd.append('file', f)
    try {
      const r = await api.post<Preview>('/imports:preview', fd)
      if (generation !== previewVersion.current) return
      setPreview(r.data); if (!submission.pending) setMapping(r.data.mapping)
    } catch { if (generation === previewVersion.current) setPreview(null) }
  }
  const start = async () => {
    if (!file || startingRef.current) return
    startingRef.current = true; setStarting(true)
    try {
      const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
      const file_hash = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('')
      const body = { type, mapping, dry_run: dryRun, filename: file.name, file_hash }
      const result = await submission.run(body, request_id => {
        const fd = new FormData(); fd.append('type', type); fd.append('file', file)
        fd.append('mapping', JSON.stringify(mapping)); fd.append('dry_run', String(dryRun)); fd.append('request_id', request_id)
        return api.post('/imports', fd)
      })
      if (result) { message.success(t('import.start')); setPreview(null); setFile(null); setFileList([]); load() }
    } catch { message.error('无法读取导入文件，请重新选择。') }
    finally { startingRef.current = false; setStarting(false) }
  }
  const download = async (id: string) => {
    const r = await fetch(`/api/v1/imports/${id}/errors.csv`, { headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` } })
    const url = URL.createObjectURL(await r.blob())
    const a = document.createElement('a'); a.href = url; a.download = `import-${id.slice(0, 8)}-errors.csv`; a.click(); URL.revokeObjectURL(url)
  }
  const missing = (fields[type] || []).filter((f) => f.required && !mapping[f.field]).map((f) => f.field)
  const color: Record<string, string> = { completed: 'green', partial: 'gold', failed: 'red', running: 'blue', queued: 'blue' }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      <Card title={t('menu.imports')}>
        <Space direction="vertical" style={{ width: '100%' }}>
          {submission.error && <Alert type="error" showIcon message={submission.error} />}
          {submission.pending && !submission.loading && <Alert type="warning" showIcon message="上次导入提交结果待确认，请重新选择原文件并重试。" />}
          <Radio.Group disabled={starting || !!submission.pending} value={type} onChange={(e) => { setType(e.target.value); if (file) doPreview(file, e.target.value) }}
            options={['companies', 'contacts', 'won_customers'].map((k) => ({ value: k, label: t(`import.${k}`) }))} optionType="button" />
          <Upload disabled={starting} accept=".csv,.xlsx,.txt" maxCount={1} fileList={fileList}
            beforeUpload={(f) => { setFile(f); setFileList([{ uid: f.uid, name: f.name, status: 'done' }]); doPreview(f); return false }}
            onRemove={() => { previewVersion.current++; setFile(null); setFileList([]); setPreview(null) }}>
            <Button icon={<UploadOutlined />}>{t('import.file')}</Button>
          </Upload>
          {preview && <>
            <Alert type="info" message={`${t('import.total')}：${preview.total}`} />
            <Table size="small" pagination={false} rowKey="field" dataSource={fields[type] || []} columns={[
              { title: t('import.target'), dataIndex: 'field', render: (v, r) => <Space>{v}{r.required && <Tag color="red">{t('import.required')}</Tag>}</Space> },
              { title: t('import.column'), render: (_, r) => <Select disabled={starting || !!submission.pending} allowClear style={{ width: 220 }} value={mapping[r.field]} placeholder={t('import.ignore')}
                options={preview.headers.map((h) => ({ value: h, label: h }))}
                onChange={(v) => { const m = { ...mapping }; if (v) m[r.field] = v; else delete m[r.field]; setMapping(m) }} /> },
              { title: '示例', render: (_, r) => { const i = preview.headers.indexOf(mapping[r.field]); return i >= 0 ? preview.sample.slice(0, 3).map((row) => row[i]).join(' / ') : '' } },
            ]} />
            <Space>
              <Checkbox disabled={starting || !!submission.pending} checked={dryRun} onChange={(e) => setDryRun(e.target.checked)}>{t('import.dryRun')}</Checkbox>
              <Button type="primary" loading={starting} disabled={missing.length > 0 || !file} onClick={start}>{t('import.start')}</Button>
            </Space>
          </>}
        </Space>
      </Card>
      <Card title={t('import.history')} size="small">
        {pollError && <Alert type="warning" showIcon message="任务状态刷新失败，正在重试。" />}
        <Table rowKey="id" size="small" dataSource={jobs} pagination={false} columns={[
          { title: '', dataIndex: 'created_at', render: (v) => dayjs(v).format('MM-DD HH:mm') },
          { title: t('import.type'), dataIndex: 'type', render: (v, r) => <Space>{t(`import.${v}`)}{r.dry_run && <Tag>预检</Tag>}</Space> },
          { title: 'File', dataIndex: 'filename' },
          { title: t('kb.status'), dataIndex: 'status', render: (v) => <Tag color={color[v]}>{t(`status.${v}`)}</Tag> },
          { title: t('import.total'), dataIndex: 'total' },
          { title: t('import.created'), dataIndex: 'created' },
          { title: t('import.merged'), dataIndex: 'merged' },
          { title: t('import.skipped'), dataIndex: 'skipped' },
          { title: t('import.failed'), dataIndex: 'failed' },
          { title: '', render: (_, r) => (r.failed || r.skipped) ? <a onClick={() => download(r.id)}>{t('import.errors')}</a> : null },
        ]} />
      </Card>
    </Space>
  )
}
