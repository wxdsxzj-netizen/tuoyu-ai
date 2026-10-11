import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import InquiriesPage from '../src/pages/Inquiries'
import InboxPage from '../src/pages/Inbox'
import RemoteSelect from '../src/RemoteSelect'
import HandoffsPage from '../src/pages/Handoffs'
import AgentPage from '../src/pages/Agent'
import CrmPage from '../src/pages/Crm'
import DeliveryPage from '../src/pages/Delivery'
import { useServerPage, PageError } from '../src/useServerPage'
import { useState } from 'react'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true, me: { tenant: { id: 't' }, permissions: [], user: { id: 'u' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (s: string) => s }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(e => style(e))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)
for (const [Page, url, sizeKey] of [[InquiriesPage, '/inquiries', 'page_size'], [InboxPage, '/conversations', 'size']] as const) {
  it(`${url} fetches the 21st record from page two`, async () => {
    const rows = Array.from({ length: 21 }, (_, i) => ({ id: String(i), peer_email: `buyer-${i + 1}@example.com`, subject: `subject-${i}`, status: 'assigned', unread_count: 0 }))
    api.get.mockImplementation((path: string, config: any) => Promise.resolve({ data: path === url ? { items: rows.slice(((config.params.page || 1) - 1) * 20, (config.params.page || 1) * 20), total: 21 } : [] }))
    render(<MemoryRouter><Page /></MemoryRouter>)
    await screen.findByText('buyer-20@example.com')
    fireEvent.click(screen.getByTitle('2'))
    await screen.findByText('buyer-21@example.com')
    expect(screen.queryByText('buyer-20@example.com')).toBeNull()
    expect(api.get).toHaveBeenLastCalledWith(url, expect.objectContaining({ params: expect.objectContaining({ page: 2, [sizeKey]: 20 }) }))
  }, 15000)
}
it('loads later team members and performs remote search without losing selection', async () => {
  api.get.mockImplementation((_url: string, cfg: any) => Promise.resolve({ data: { items: cfg.params.page === 2 ? [{ id: '21', name: 'Member 21' }] : Array.from({ length: 20 }, (_, i) => ({ id: String(i), name: `Member ${i}` })), total: 21 } }))
  const selected = vi.fn()
  render(<RemoteSelect virtual={false} aria-label="Team" url="/users" onChange={selected} style={{ width: 300 }} />)
  await waitFor(() => expect(api.get).toHaveBeenCalled())
  fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Team' }))
  fireEvent.click(await screen.findByText('加载更多'))
  fireEvent.click(await screen.findByText('Member 21', { selector: '.ant-select-item-option-content' }))
  expect(selected).toHaveBeenCalledWith('21', expect.objectContaining({ id: '21' }))
  fireEvent.change(screen.getByRole('combobox', { name: 'Team' }), { target: { value: 'Alice' } })
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/users', { params: { page: 1, size: 20, q: 'Alice' } }))
}, 15000)

for (const [Page, url, tab, sizeKey] of [
  [HandoffsPage, '/handoffs', '', 'size'], [AgentPage, '/agent/tasks', '', 'page_size'],
  [CrmPage, '/accounts', '', 'size'], [CrmPage, '/opportunities', 'p4.pipeline', 'size'],
  [CrmPage, '/quotes', 'p4.quotes', 'size'], [CrmPage, '/pis', 'p4.pis', 'size'],
  [DeliveryPage, '/delivery/messages', 'p3.outbound', 'size'], [DeliveryPage, '/delivery/inbound', 'p3.inbound', 'size'],
] as const) {
  it(`${url} consumes server totals and requests later pages`, async () => {
    const rows = Array.from({ length: 21 }, (_, i) => ({ id: String(i), summary: `row-${i + 1}`, input_text: `row-${i + 1}`, name: `row-${i + 1}`,
      quote_no: `row-${i + 1}`, pi_no: `row-${i + 1}`, to: `row-${i + 1}`, from: `row-${i + 1}`, total: 100, amount: 100, currency: 'USD',
      stage: 'new_inquiry', status: 'draft', created_at: '2026-01-01', doc_version: 1 }))
    api.get.mockImplementation((path: string, cfg: any) => Promise.resolve({ data: path === url
      ? { items: rows.slice(((cfg.params.page || 1) - 1) * 20, (cfg.params.page || 1) * 20), total: 21, balance: 100 }
      : { items: [], total: 0 } }))
    render(<MemoryRouter><Page /></MemoryRouter>)
    if (tab) fireEvent.click(screen.getByRole('tab', { name: tab }))
    await screen.findByText(/^row-20(?: v1)?$/)
    fireEvent.click(screen.getByTitle('2'))
    await screen.findByText(/^row-21(?: v1)?$/)
    expect(api.get).toHaveBeenCalledWith(url, expect.objectContaining({ params: expect.objectContaining({ page: 2, [sizeKey]: 20 }) }))
  }, 20000)
}
function RaceHarness() {
  const [status, setStatus] = useState('old')
  const page = useServerPage('/example', { status })
  return <><button onClick={() => setStatus('new')}>Change filter</button><PageError page={page} />{page.rows.map(row => <div key={row.id}>{row.name}</div>)}</>
}
it('ignores an old filter response and offers a retry for the current failure', async () => {
  let finish!: (value: any) => void
  api.get.mockImplementation((_url: string, cfg: any) => cfg.params.status === 'old' ? new Promise(resolve => { finish = resolve }) : Promise.reject(new Error('offline')))
  render(<RaceHarness />)
  fireEvent.click(screen.getByText('Change filter'))
  await screen.findByText('列表加载失败')
  await act(async () => finish({ data: { items: [{ id: 'old', name: 'Wrong result' }], total: 1 } }))
  expect(screen.queryByText('Wrong result')).toBeNull()
  api.get.mockResolvedValue({ data: { items: [{ id: 'new', name: 'Current result' }], total: 1 } })
  fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }))
  await screen.findByText('Current result')
})
