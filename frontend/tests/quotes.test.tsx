import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { QuoteEditor } from '../src/pages/Crm'

const mocks = vi.hoisted(() => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn() }, canEditCost: false }))
vi.mock('../src/api/client', () => ({ api: mocks.api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: (permission: string) => permission === 'price.floor' ? mocks.canEditCost : true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
const quote = { id: 'q', version: 4, account_id: 'a', incoterm: 'FOB', currency: 'USD', language: 'en',
  items: [{ id: 'line-a', sku_id: 'sku-a', name: 'Product', sku: 'SKU-A', qty: 2, unit: 'pcs', unit_price: 100 }],
  discount_pct: 10, freight: 20, insurance: 5, commission: 10, notes: 'Original' }

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks()
  mocks.canEditCost = false
  mocks.api.get.mockImplementation((path: string) => Promise.resolve({ data: { items: path === '/accounts' ? [{ id: 'a', name: 'Buyer' }] : [] } }))
  mocks.api.put.mockResolvedValue({ data: {} })
})
afterEach(cleanup)

it('hides cost editing and invented margin for sales', () => {
  render(<QuoteEditor quote={quote} onDone={vi.fn()} />)
  expect(screen.queryByPlaceholderText('p4.unitCost')).toBeNull()
  expect(screen.queryByText(/p4.margin/)).toBeNull()
})

it('preserves server line identity and version when saving an ordinary notes edit', async () => {
  const done = vi.fn()
  render(<QuoteEditor quote={quote} onDone={done} />)
  fireEvent.change(screen.getByLabelText('p4.notes'), { target: { value: 'Changed note' } })
  fireEvent.click(screen.getByText('p4.save'))
  await waitFor(() => expect(mocks.api.put).toHaveBeenCalledTimes(1))
  const body = mocks.api.put.mock.calls[0][1]
  expect(body.version).toBe(4)
  expect(body.items[0]).toMatchObject({ id: 'line-a', sku_id: 'sku-a' })
  expect(body.items[0].unit_cost).toBeUndefined()
  expect(done).toHaveBeenCalledTimes(1)
})

it('includes freight, insurance and commission in the visible calculation', () => {
  mocks.canEditCost = true
  render(<QuoteEditor quote={{ ...quote, items: [{ ...quote.items[0], unit_cost: 50 }] }} onDone={vi.fn()} />)
  expect(screen.getByText('p4.total: 205.00')).toBeTruthy()
  expect(screen.getByText(/p4.margin: 34.1%/)).toBeTruthy()
})

it('keeps a pending save locked against a second submission', async () => {
  mocks.api.put.mockReturnValue(new Promise(() => {}))
  render(<QuoteEditor quote={quote} onDone={vi.fn()} />)
  fireEvent.click(screen.getByText('p4.save'))
  await waitFor(() => expect(mocks.api.put).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByText('p4.save'))
  await new Promise((resolve) => setTimeout(resolve, 40))
  expect(mocks.api.put).toHaveBeenCalledTimes(1)
})

it('rounds fractional cents half up without floating point drift', () => {
  mocks.canEditCost = true
  render(<QuoteEditor quote={{ ...quote, discount_pct: 0, freight: 0, insurance: 0, commission: 0,
    items: [{ ...quote.items[0], qty: 1, unit_price: 1.005, unit_cost: 0 }] }} onDone={vi.fn()} />)
  expect(screen.getByText('p4.total: 1.01')).toBeTruthy()
})

it('uses current configured approval thresholds', async () => {
  mocks.canEditCost = true
  mocks.api.get.mockImplementation((path: string) => Promise.resolve({ data: path === '/crm/settings/quote'
    ? { margin_manager: 40, margin_owner: 8, discount_manager: 50 } : { items: [] } }))
  render(<QuoteEditor quote={{ ...quote, items: [{ ...quote.items[0], unit_cost: 50 }] }} onDone={vi.fn()} />)
  await waitFor(() => expect(screen.getByText(/p4.needsApproval/)).toBeTruthy())
})
