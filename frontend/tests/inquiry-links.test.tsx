import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import InquiriesPage from '../src/pages/Inquiries'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ me: { permissions: [], user: { id: 'sales' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
afterEach(cleanup)
it('offers each merged conversation with its own recipient and exact destination', async () => {
  const item = { id: 'inquiry', peer_email: 'buyer@example.org', status: 'assigned', conversation_id: 'first', conversations: [
    { id: 'first', peer_email: 'a@example.org', subject: 'Topic A' },
    { id: 'second', peer_email: 'b@example.org', subject: 'Topic B' },
  ] }
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/inquiries' ? { items: [item], total: 1 } : url === '/inquiries/inquiry' ? item : [] }))
  render(<InquiriesPage />)
  fireEvent.click(await screen.findByText('buyer@example.org'))
  expect((await screen.findByRole('link', { name: '打开会话：a@example.org · Topic A' })).getAttribute('href')).toBe('/inbox?c=first')
  expect(screen.getByRole('link', { name: '打开会话：b@example.org · Topic B' }).getAttribute('href')).toBe('/inbox?c=second')
})
