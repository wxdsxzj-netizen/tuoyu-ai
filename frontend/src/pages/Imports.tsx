import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Radio, Select, Space, Table, Tag, Upload, message } from 'antd'
import type { UploadFile } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import dayjs from 'dayjs'
import { api, TOKEN_KEY } from '../api/client'

interface Job { id: string; type: string; filename: string; status: string; total: number; created: number; merged: number; skipped: number; failed: number; dry_run: boolean; created_at: string }
interface Preview { headers: string[]; sample: string[][]; total: number; mapping: Record<string, string>; missing_required: string[] }
interface FieldDef { field: string; required: boolean }

export default function ImportsPage() {
  const { t } = useTranslation()
  const [type, setType] = useState('companies')
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [fields, setFields] = useState<Record<string, FieldDef[]>>({})
  const [dryRun, setDryRun] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const timer = useRef<number>()

  const load = useCallback(async () => { const r = await api.get<Job[]>('/imports'); setJobs(r.data); return r.data }, [])
  useEffect(() => { load(); api.get('/imports/fields').then((r) => setFields(r.data)) }, [load])
  useEffect(() => () => window.clearInterval(timer.current), [])

  const doPreview = async (f: File, tp = type) => {
    const fd = new FormData(); fd.append('type', tp); fd.append('file', f)
    const r = await api.post<Preview>('/imports:preview', fd)
    setPreview(r.data); setMapping(r.data.mapping)
  }
  const start = async () => {
    if (!file) return
    const fd = new FormData(); fd.append('type', type); fd.append('file', file)
    fd.append('mapping', JSON.stringify(mapping)); fd.append('dry_run', String(dryRun))
    await api.post('/imports', fd)
    message.success(t('import.start')); setPreview(null); setFile(null); setFileList([])
    window.clearInterval(timer.current)
    timer.current = window.setInterval(async () => {
      const js = await load()
      if (!js.some((j) => ['queued', 'running'].includes(j.status))) window.clearInterval(timer.current)
    }, 1500)
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
          <Radio.Group value={type} onChange={(e) => { setType(e.target.value); if (file) doPreview(file, e.target.value) }}
            options={['companies', 'contacts', 'won_customers'].map((k) => ({ value: k, label: t(`import.${k}`) }))} optionType="button" />
          <Upload accept=".csv,.xlsx,.txt" maxCount={1} fileList={fileList}
            beforeUpload={(f) => { setFile(f); setFileList([{ uid: f.uid, name: f.name, status: 'done' }]); doPreview(f); return false }}
            onRemove={() => { setFile(null); setFileList([]); setPreview(null) }}>
            <Button icon={<UploadOutlined />}>{t('import.file')}</Button>
          </Upload>
          {preview && <>
            <Alert type="info" message={`${t('import.total')}：${preview.total}`} />
            <Table size="small" pagination={false} rowKey="field" dataSource={fields[type] || []} columns={[
              { title: t('import.target'), dataIndex: 'field', render: (v, r) => <Space>{v}{r.required && <Tag color="red">{t('import.required')}</Tag>}</Space> },
              { title: t('import.column'), render: (_, r) => <Select allowClear style={{ width: 220 }} value={mapping[r.field]} placeholder={t('import.ignore')}
                options={preview.headers.map((h) => ({ value: h, label: h }))}
                onChange={(v) => { const m = { ...mapping }; if (v) m[r.field] = v; else delete m[r.field]; setMapping(m) }} /> },
              { title: '示例', render: (_, r) => { const i = preview.headers.indexOf(mapping[r.field]); return i >= 0 ? preview.sample.slice(0, 3).map((row) => row[i]).join(' / ') : '' } },
            ]} />
            <Space>
              <Checkbox checked={dryRun} onChange={(e) => setDryRun(e.target.checked)}>{t('import.dryRun')}</Checkbox>
              <Button type="primary" disabled={missing.length > 0} onClick={start}>{t('import.start')}</Button>
            </Space>
          </>}
        </Space>
      </Card>
      <Card title={t('import.history')} size="small">
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
