import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import MailboxesPage from '../src/pages/Mailboxes'
import OAuthConnect from '../src/OAuthConnect'
import PlatformPage from '../src/pages/Platform'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: (p: string) => p === 'mailbox.manage' }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (s: string) => s }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(e => style(e))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  api.get.mockReset(); api.post.mockReset()
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('oauth') ? { stub: true, authorize_url: null, note: '当前尚未接通真实邮箱', next_steps: ['完成服务商验证'] } : [] }))
  api.post.mockImplementation(api.get)
})
afterEach(cleanup)
for (const [Page, label] of [[MailboxesPage, 'p3.bindGoogle'], [PlatformPage, 'b78.oauthGoogle']] as const) {
  it(`${label} explains unavailable connection and supports cancellation without creating a mailbox`, async () => {
    render(<Page />)
    fireEvent.click(await screen.findByText(label))
    expect(await screen.findByText('当前尚未接通真实邮箱')).toBeTruthy()
    expect(screen.getByText('完成服务商验证')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /关\s*闭/ }))
    await waitFor(() => expect(screen.queryByText('当前尚未接通真实邮箱')).toBeNull())
    expect(api.post).not.toHaveBeenCalled()
  })
}
it('shows a recoverable error and retries the same provider', async () => {
  api.get.mockImplementation((url: string) => url.includes('oauth') ? Promise.reject(new Error('offline')) : Promise.resolve({ data: [] }))
  render(<MailboxesPage />)
  fireEvent.click(await screen.findByText('p3.bindMs'))
  expect(await screen.findByText('无法获取授权信息，请重试。')).toBeTruthy()
  api.get.mockResolvedValue({ data: { stub: true, note: '暂不可用', next_steps: [] } })
  fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }))
  expect(await screen.findByText('暂不可用')).toBeTruthy()
  expect(api.get).toHaveBeenLastCalledWith('/mailboxes/oauth/microsoft:start')
})

it('offers an explicit provider authorization link and rejects unexpected hosts', async () => {
  api.get.mockResolvedValue({ data: { stub: false, authorize_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=synthetic', next_steps: [] } })
  const { unmount } = render(<OAuthConnect provider="google">Connect</OAuthConnect>)
  fireEvent.click(screen.getByText('Connect'))
  expect((await screen.findByRole('link', { name: '继续服务商授权' })).getAttribute('href')).toContain('https://accounts.google.com/')
  unmount()
  api.get.mockResolvedValue({ data: { stub: false, authorize_url: 'https://untrusted.example/path' } })
  render(<OAuthConnect provider="google">Connect</OAuthConnect>)
  fireEvent.click(screen.getByText('Connect'))
  await screen.findByText('当前尚未接通真实邮箱。')
  expect(screen.queryByRole('link', { name: '继续服务商授权' })).toBeNull()
})
