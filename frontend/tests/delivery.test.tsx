import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import DeliveryPage from '../src/pages/Delivery'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks()
  api.get.mockImplementation((path: string) => Promise.resolve({ data: { items: path === '/delivery/intents' ? [
    { id: 'receipt-1', state: 'unknown', can_reconcile: true, message_id: '<one@synthetic.test>', updated_at: '2026-10-11T00:00:00Z' },
    { id: 'receipt-2', state: 'sending', can_reconcile: false, message_id: '<two@synthetic.test>', updated_at: '2026-10-11T00:00:00Z' },
  ] : [], total: 21 } }))
})
afterEach(cleanup)

it('requires an explicit provider conclusion and evidence, and submits the selected receipt only once', async () => {
  let resolve!: (value: object) => void
  api.post.mockReturnValue(new Promise((done) => { resolve = done }))
  render(<DeliveryPage />)
  const buttons = await screen.findAllByText('记录核查结论')
  expect(buttons[1].closest('button')!.disabled).toBe(true)
  fireEvent.click(buttons[0])
  fireEvent.click(screen.getByText('保存核查结论'))
  await act(async () => {})
  expect(api.post).not.toHaveBeenCalled()
  fireEvent.click(screen.getByLabelText('已接收，不重发'))
  fireEvent.change(screen.getByLabelText('日志时间、记录编号及核查依据'), { target: { value: 'Synthetic mail provider accepted event 12345' } })
  fireEvent.click(screen.getByText('保存核查结论'))
  fireEvent.click(screen.getByText('保存核查结论'))
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
  expect(api.post).toHaveBeenCalledWith('/delivery/intents/receipt-1:reconcile', {
    outcome: 'sent', evidence: 'Synthetic mail provider accepted event 12345',
  })
  await act(async () => resolve({ data: {} }))
})

it('fetches the next server page of unresolved sends', async () => {
  render(<DeliveryPage />)
  await screen.findByText('<one@synthetic.test>')
  fireEvent.click(screen.getByTitle('2'))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith('/delivery/intents', { params: { page: 2, size: 20 } }))
})
