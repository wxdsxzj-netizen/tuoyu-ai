import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import ProductsPage from '../src/pages/Products'
import DiscoverPage from '../src/pages/Discover'
import CrmPlusPage from '../src/pages/CrmPlus'
import { MemoryRouter } from 'react-router-dom'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }))
const auth = vi.hoisted(() => ({ manage: true }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => auth.manage, me: { tenant: { id: 't' }, user: { id: 'u' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (s: string) => s }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(e => style(e))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
afterEach(cleanup)
beforeEach(() => { vi.clearAllMocks(); auth.manage = true; api.post.mockResolvedValue({ data: {} }); api.put.mockResolvedValue({ data: {} }); api.get.mockResolvedValue({ data: [] }) })
it('edits an existing product while preserving unedited structured fields', async () => {
  const product = { id: 'p', name_zh: 'Valve', params: { pressure: 'PN16' }, certifications: [], translations: { de: { locked: true, text: 'Ventil' } } }
  api.get.mockResolvedValue({ data: [product] })
  render(<ProductsPage />)
  fireEvent.click(await screen.findByRole('button', { name: '编辑产品' }))
  const modal = await screen.findByRole('dialog')
  fireEvent.change(within(modal).getByLabelText('b2.nameZh'), { target: { value: 'New Valve' } })
  fireEvent.click(within(modal).getByText('OK'))
  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/products/p', expect.objectContaining({ name_zh: 'New Valve', params: product.params, translations: product.translations })))
}, 20000)
it('exposes SKU creation and editing inside a product', async () => {
  const product = { id: 'p', name_zh: 'Valve', skus: [{ id: 's', sku: 'SKU-1', factory_price: 20, spec: { size: 'M' }, tier_prices: [] }] }
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/products' ? [product] : product }))
  render(<ProductsPage />)
  fireEvent.click(await screen.findByText('Valve'))
  fireEvent.click(await screen.findByRole('button', { name: '编辑 SKU' }))
  const modal = screen.getAllByRole('dialog').at(-1)!
  fireEvent.change(within(modal).getByLabelText('SKU 编码'), { target: { value: 'SKU-2' } })
  fireEvent.click(within(modal).getByText('OK'))
  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/products/p/skus/s', expect.objectContaining({ sku: 'SKU-2', spec: { size: 'M' } })))
  fireEvent.click(await screen.findByRole('button', { name: '新增 SKU' }))
  const create = screen.getAllByRole('dialog').at(-1)!
  fireEvent.change(within(create).getByLabelText('SKU 编码'), { target: { value: 'SKU-3' } })
  fireEvent.click(within(create).getByText('OK'))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/products/p/skus', expect.objectContaining({ sku: 'SKU-3' })))
}, 20000)
it('adds a selected contact to an existing static list', async () => {
  const list = { id: 'l', name: 'Static', kind: 'static', object_type: 'contact', members: [] }
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/lists' ? [list] : url === '/lists/l' ? list : url === '/contacts' ? { items: [{ id: 'c', full_name: 'Buyer', email: 'buyer@example.com' }], total: 1 } : [] }))
  render(<DiscoverPage />)
  fireEvent.click(screen.getByRole('tab', { name: 'b3.tabLists' }))
  fireEvent.click(await screen.findByText('b3.view'))
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: '选择名单成员' }))
  fireEvent.click(await screen.findByText('Buyer · buyer@example.com', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(screen.getByRole('button', { name: '添加成员' }))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/lists/l/members', { add: ['c'] }))
}, 20000)
for (const kind of ['samples', 'orders']) {
  it(`creates ${kind} through the existing account-scoped endpoint`, async () => {
    api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/accounts' ? { items: [{ id: 'a', name: 'Acme' }], total: 1 } : [] }))
    render(<MemoryRouter initialEntries={[`/?tab=${kind}`]}><CrmPlusPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: kind === 'samples' ? '申请样品' : '创建订单' }))
    const modal = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(modal).getByLabelText('客户'))
    fireEvent.click(await screen.findByText('Acme', { selector: '.ant-select-item-option-content' }))
    if (kind === 'samples') fireEvent.change(within(modal).getByLabelText('品名'), { target: { value: 'Valve' } })
    else fireEvent.change(within(modal).getByLabelText('订单金额'), { target: { value: '120.50' } })
    fireEvent.click(within(modal).getByText('OK'))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/' + kind, expect.objectContaining(kind === 'samples' ? { account_id: 'a', items: [{ name: 'Valve', qty: 1 }] } : { account_id: 'a', total_minor: 12050, currency: 'USD' }), expect.anything()))
  }, 20000)
}
it('hides write entrypoints from a read-only user', async () => {
  auth.manage = false
  render(<MemoryRouter><CrmPlusPage /></MemoryRouter>)
  expect(screen.queryByRole('button', { name: '申请样品' })).toBeNull()
  fireEvent.click(screen.getByRole('tab', { name: 'b2.orders' }))
  expect(screen.queryByRole('button', { name: '创建订单' })).toBeNull()
})
it('retains a timed-out creation intent across remount for the same request', async () => {
  sessionStorage.clear()
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/accounts' ? { items: [{ id: 'a', name: 'Acme' }], total: 1 } : [] }))
  api.post.mockRejectedValueOnce(new Error('offline'))
  const view = render(<MemoryRouter initialEntries={['/?tab=orders']}><CrmPlusPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: '创建订单' }))
  let modal = await screen.findByRole('dialog')
  fireEvent.mouseDown(within(modal).getByLabelText('客户'))
  fireEvent.click(await screen.findByText('Acme', { selector: '.ant-select-item-option-content' }))
  fireEvent.change(within(modal).getByLabelText('订单金额'), { target: { value: '100' } })
  fireEvent.click(within(modal).getByText('OK'))
  await screen.findByText('结果待确认，请重试原请求或核对记录，勿重复创建。')
  const original = api.post.mock.calls[0]
  view.unmount()
  render(<MemoryRouter initialEntries={['/?tab=orders']}><CrmPlusPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: '创建订单' }))
  modal = await screen.findByRole('dialog')
  fireEvent.click(within(modal).getByText('OK'))
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
  expect(api.post.mock.calls[1]).toEqual(original)
}, 20000)
it('records feedback before advancing a received sample', async () => {
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/samples' ? [{ id: 'sample', items: [], fee_type: 'free', status: 'received' }] : [] }))
  render(<MemoryRouter><CrmPlusPage /></MemoryRouter>)
  fireEvent.click(await screen.findByText('b2.sm_feedback'))
  fireEvent.change(await screen.findByLabelText('样品反馈'), { target: { value: '尺寸符合要求' } })
  fireEvent.click(within(screen.getByRole('dialog')).getByText('OK'))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/samples/sample:move', { to: 'feedback', feedback: '尺寸符合要求' }))
})
