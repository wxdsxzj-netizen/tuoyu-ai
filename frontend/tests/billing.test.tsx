import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import BillingPage from '../src/pages/Billing'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

beforeAll(() => {
  const getComputedStyle = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => getComputedStyle(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
afterEach(cleanup)

function summary(settlement: object | null) {
  api.get.mockImplementation((path: string) => Promise.resolve({ data: path === '/billing/summary'
    ? { settlement, low_balance: false, balance: 80, held: 20, used_this_month: 0, granted_total: 100,
      unpaid_invoices: 0, prices: {}, used_by_action: {} }
    : path === '/billing/invoices' ? [] : { items: [] } }))
}

it('explains the pending month and limited paid-work restriction without requesting a topup', async () => {
  summary({ period: '2026-01', blocked: true, reserved: 20 })
  render(<BillingPage />)
  await screen.findByText('月结等待 2026-01 的预占结清')
  expect(screen.getByText('还有 20 点预占。请先完成或取消原任务，再补跑月结；新付费任务暂停，其他服务不受影响。')).toBeTruthy()
  expect(screen.queryByText('p5.lowBalance')).toBeNull()
})

it('does not show a paid-work restriction for a tenant without pending reservations', async () => {
  summary({ period: '2026-01', blocked: false, reserved: 0 })
  render(<BillingPage />)
  await screen.findByText('p5.heldNow')
  expect(screen.queryByText(/月结等待/)).toBeNull()
})
