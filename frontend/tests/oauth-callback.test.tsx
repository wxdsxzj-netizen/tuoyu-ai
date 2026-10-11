import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import MailboxOAuthCallback from '../src/pages/MailboxOAuthCallback'
const api = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
beforeEach(() => {
  api.post.mockReset()
  window.history.replaceState({}, '', '/mailboxes/oauth/google/callback?state=synthetic-state&code=synthetic-code')
})
afterEach(cleanup)
function Page() {
  return <StrictMode><MemoryRouter initialEntries={['/mailboxes/oauth/google/callback']}><Routes>
    <Route path="/mailboxes/oauth/:provider/callback" element={<MailboxOAuthCallback />} />
  </Routes></MemoryRouter></StrictMode>
}
it('consumes one callback under StrictMode and removes authorization data from history', async () => {
  api.post.mockResolvedValue({ data: { address: 'bound@example.com' } })
  render(<Page />)
  await screen.findByText('邮箱授权已连接')
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(api.post).toHaveBeenCalledWith('/mailboxes/oauth/google:callback', { state: 'synthetic-state', code: 'synthetic-code', error: undefined })
  expect(window.location.search).toBe('')
})
it('reports provider cancellation without pretending a mailbox was bound', async () => {
  window.history.replaceState({}, '', '/mailboxes/oauth/google/callback?state=synthetic-state&error=access_denied')
  api.post.mockResolvedValue({ data: { cancelled: true } })
  render(<Page />)
  await screen.findByText('已取消邮箱授权，本次没有绑定邮箱。')
  expect(screen.queryByText('邮箱授权已连接')).toBeNull()
})
it('retries an uncertain callback with its original identity and shows no false success', async () => {
  api.post.mockRejectedValueOnce(new Error('offline'))
  render(<Page />)
  await screen.findByText('邮箱连接未完成')
  expect(screen.queryByText('邮箱授权已连接')).toBeNull()
  api.post.mockResolvedValue({ data: { address: 'bound@example.com' } })
  fireEvent.click(screen.getByRole('button', { name: '重试确认' }))
  await screen.findByText('邮箱授权已连接')
  expect(api.post.mock.calls[1]).toEqual(api.post.mock.calls[0])
})
it('requires a fresh authorization when callback parameters are missing', async () => {
  window.history.replaceState({}, '', '/mailboxes/oauth/google/callback')
  render(<Page />)
  await waitFor(() => expect(screen.getByText('授权信息缺失或已过期，请返回发件箱重新授权。')).toBeTruthy())
  expect(api.post).not.toHaveBeenCalled()
})
