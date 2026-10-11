import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import DsrPage from '../src/pages/Dsr'
import IcpPage from '../src/pages/Icp'
import AiLabPage from '../src/pages/AiLab'
import DiscoverPage from '../src/pages/Discover'
import { MemoryRouter } from 'react-router-dom'
import { Modal } from 'antd'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => { vi.clearAllMocks(); api.get.mockResolvedValue({ data: [] }); api.post.mockResolvedValue({ data: {} }) })
afterEach(() => { cleanup(); Modal.destroyAll() })

it('shows all 20 server rows and fetches the 21st row on page two', async () => {
  const rows = Array.from({ length: 21 }, (_, i) => ({ id: String(i), subject_email: `subject-${i + 1}@example.com`, type: 'access', status: 'received', received_at: '2026-01-01', days_left: 10 }))
  api.get.mockImplementation((_url, cfg) => Promise.resolve({ data: { items: rows.slice((cfg.params.page - 1) * 20, cfg.params.page * 20), total: 21 } }))
  render(<DsrPage />)
  await screen.findByText('subject-20@example.com')
  fireEvent.click(screen.getByTitle('2'))
  await screen.findByText('subject-21@example.com')
  expect(screen.queryByText('subject-20@example.com')).toBeNull()
  expect(api.get).toHaveBeenLastCalledWith('/dsr', { params: { page: 2, status: 'open', size: 20 } })
})

it('clears all previous ICP array fields before creating a new ICP', async () => {
  const fields = ['industries', 'countries', 'employee_ranges', 'roles', 'buying_traits', 'exclusions']
  api.get.mockResolvedValue({ data: [{ id: 'old', name: 'Old', description: 'Old description', status: 'active', version: 1, ...Object.fromEntries(fields.map(f => [f, ['old-' + f]])) }] })
  render(<IcpPage />)
  fireEvent.click(await screen.findByText('icp.edit'))
  await screen.findByRole('dialog')
  fireEvent.click(within(screen.getByRole('dialog')).getByText('Cancel'))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(screen.getByText('icp.create'))
  fireEvent.change(within(await screen.findByRole('dialog')).getByLabelText('icp.name'), { target: { value: 'New' } })
  fireEvent.click(within(screen.getByRole('dialog')).getByText('OK'))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/icps', expect.objectContaining(Object.fromEntries(fields.map(f => [f, []])))))
})

it('requires a fresh ICP choice after canceling an earlier learning suggestion', async () => {
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/icps' ? [{ id: 'icp-a', name: 'ICP A' }] : url === '/learning' ? [{ id: 'report', kind: 'learning', period: 'test', content: { dims: {}, suggestions: [{ id: 'one', type: 'icp_country', value: 'First' }, { id: 'two', type: 'icp_country', value: 'Second' }] } }] : [] }))
  render(<AiLabPage />)
  fireEvent.click(screen.getByRole('tab', { name: 'b5.tabLearn' }))
  fireEvent.click((await screen.findAllByText('b5.apply'))[0])
  let dialog = await screen.findByRole('dialog')
  fireEvent.mouseDown(within(dialog).getByRole('combobox'))
  fireEvent.click(await screen.findByText('ICP A', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(within(dialog).getByText('Cancel'))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(screen.getAllByText('b5.apply')[1])
  dialog = await screen.findByRole('dialog')
  const ok = within(dialog).getByText('OK').closest('button')!
  if (!ok.disabled) fireEvent.click(ok)
  await new Promise(resolve => setTimeout(resolve, 100))
  expect(api.post).not.toHaveBeenCalled()
  fireEvent.mouseDown(within(dialog).getByRole('combobox'))
  fireEvent.click(await screen.findByText('ICP A', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(within(dialog).getByText('OK'))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/learning/report:apply', { suggestion_id: 'two', icp_id: 'icp-a' }))
}, 20000)

it('submits the canonical catch_all email status from the dynamic-list selector', async () => {
  render(<MemoryRouter><DiscoverPage /></MemoryRouter>)
  fireEvent.click(screen.getByRole('tab', { name: 'b3.tabLists' }))
  fireEvent.change(await screen.findByPlaceholderText('b3.name'), { target: { value: 'Catch all list' } })
  fireEvent.mouseDown(document.querySelector('#kind')!)
  fireEvent.click(await screen.findByText('b3.k_dynamic', { selector: '.ant-select-item-option-content' }))
  fireEvent.mouseDown(document.querySelector('#email_status')!)
  fireEvent.click(await screen.findByText('catch_all', { selector: '.ant-select-item-option-content' }))
  fireEvent.click(screen.getByText('b3.createList'))
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/lists', expect.objectContaining({ filters: expect.objectContaining({ email_status: ['catch_all'] }) })))
}, 20000)
