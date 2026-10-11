import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { Orders } from '../src/pages/CrmPlus'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true, me: { tenant: { id: 'tenant-a' }, user: { id: 'user-a' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/orders'
    ? [{ id: 'order-a', order_no: 'Synthetic', total_minor: 30000, paid_minor: 0, currency: 'USD', status: 'confirmed', payments: [] }]
    : [] }))
  api.post.mockReturnValue(new Promise(() => {}))
})
afterEach(cleanup)

async function openPayment() {
  render(<Orders />)
  fireEvent.click(await screen.findByText('b2.addPayment'))
}

it('creates a durable payment intent and prevents duplicate pending submissions', async () => {
  await openPayment()
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } })
  fireEvent.click(screen.getByText('OK').closest('button')!)
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
  expect(api.post.mock.calls[0][1].request_id).toMatch(/^[0-9a-f-]{36}$/)
  fireEvent.click(screen.getByText('OK').closest('button')!)
  expect(api.post).toHaveBeenCalledTimes(1)
  expect((screen.getByRole('spinbutton') as HTMLInputElement).disabled).toBe(true)
}, 15000)

it('keeps the original receipt identity and amount across timeout and remount', async () => {
  api.post.mockRejectedValueOnce(new Error('simulated timeout'))
  await openPayment()
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } })
  fireEvent.click(screen.getByText('OK').closest('button')!)
  await screen.findByText('上次收款登记结果待确认，请重试原请求或查看订单记录。')
  const original = api.post.mock.calls[0][1]
  cleanup()
  await openPayment()
  expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('100.00')
  expect((screen.getByRole('spinbutton') as HTMLInputElement).disabled).toBe(true)
  api.post.mockResolvedValueOnce({ data: {} })
  fireEvent.click(screen.getByText('OK').closest('button')!)
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
  expect(api.post.mock.calls[1][1]).toEqual(original)
  await waitFor(() => expect(sessionStorage.length).toBe(0))
}, 15000)

it('clears confirmed receipts and starts an intentional new installment with an empty amount', async () => {
  api.post.mockResolvedValue({ data: {} })
  await openPayment()
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } })
  fireEvent.click(screen.getByText('OK').closest('button')!)
  await waitFor(() => expect(sessionStorage.length).toBe(0))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(screen.getByRole('button', { name: 'b2.addPayment' }))
  expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('')
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } })
  fireEvent.click(screen.getByText('OK').closest('button')!)
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
  expect(api.post.mock.calls[0][1].request_id).not.toBe(api.post.mock.calls[1][1].request_id)
}, 15000)
