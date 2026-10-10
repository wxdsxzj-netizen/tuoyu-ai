/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { Badge, Button, List, Popover, Tag, Typography } from 'antd'
import { BellOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

const LEVEL: Record<string, string> = { urgent: 'red', warn: 'orange', info: 'blue' }

export default function NotificationBell() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [data, setData] = useState<any>({ items: [], unread: 0 })
  const load = useCallback(async () => { try { setData((await api.get('/notifications', { params: { size: 15 } })).data) } catch { /* 静默 */ } }, [])
  useEffect(() => { load(); const h = setInterval(load, 60000); return () => clearInterval(h) }, [load])
  const go = async (n: any) => {
    if (!n.read_at) await api.post(`/notifications/${n.id}:read`)
    load()
    if (n.action_url) nav(n.action_url)
  }
  const content = (
    <div style={{ width: 380 }}>
      <List size="small" dataSource={data.items} locale={{ emptyText: t('p4.noNotif') }} renderItem={(n: any) => (
        <List.Item onClick={() => go(n)} style={{ cursor: 'pointer', opacity: n.read_at ? 0.55 : 1 }}>
          <List.Item.Meta title={<><Tag color={LEVEL[n.level]}>{n.code}</Tag>{n.title}{n.merged_count > 1 && <Tag>×{n.merged_count}</Tag>}</>}
            description={<><Typography.Paragraph ellipsis={{ rows: 2 }} style={{ margin: 0 }}>{n.body}</Typography.Paragraph>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{new Date(n.created_at).toLocaleString()}</Typography.Text></>} />
        </List.Item>)} />
      <Button size="small" type="link" disabled={!data.unread} onClick={async () => { await api.post('/notifications:read-all'); load() }}>{t('p4.readAll')}</Button>
    </div>)
  return (
    <Popover content={content} title={t('p4.notifications')} trigger="click" placement="bottomRight" onOpenChange={(o) => o && load()}>
      <Badge count={data.unread} size="small"><BellOutlined style={{ fontSize: 18, cursor: 'pointer' }} /></Badge>
    </Popover>
  )
}
