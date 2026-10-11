import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { Modal, message } from 'antd'
import ApprovalCenterPage from '../src/pages/ApprovalCenter'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
beforeEach(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
let confirmation: Parameters<typeof Modal.confirm>[0]
let rows: { id: string; type: string; title: string; unified: boolean }[]
beforeEach(() => {
  vi.clearAllMocks()
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
  rows = [1, 2, 3].map(i => ({ id: 'request-' + i, type: i === 3 ? 'export' : 'other', title: 'Request ' + i, unified: true }))
  api.get.mockImplementation(async path => ({ data: path === '/approvals-center' ? rows : [] }))
  vi.spyOn(Modal, 'confirm').mockImplementation(cfg => { confirmation = cfg; return { destroy: vi.fn(), update: vi.fn() } })
  vi.spyOn(message, 'success').mockImplementation(vi.fn())
  vi.spyOn(message, 'warning').mockImplementation(vi.fn())
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })
async function approveAll() {
  render(<MemoryRouter><ApprovalCenterPage /></MemoryRouter>)
  await screen.findByText('Request 1')
  fireEvent.click(screen.getAllByRole('checkbox')[0])
  fireEvent.click(screen.getByText('b1.batchApprove'))
  await act(async () => { await confirmation.onOk?.() })
}

it('groups unified approvals by type and preserves failed selections without a success toast', async () => {
  api.post.mockImplementation(async (path, body) => {
    if (path.endsWith('batch-decide')) return { data: Object.fromEntries(body.ids.map((id: string) => [id, id === 'request-2' ? 'AUTH_FORBIDDEN' : 'approved'])) }
    return { data: { status: 'approved' } }
  })
  await approveAll()
  const batch = api.post.mock.calls.find(([p]) => p === '/approval-requests:batch-decide')!
  expect(batch[1].ids).toEqual(['request-1', 'request-2'])
  expect(api.post).toHaveBeenCalledWith('/approval-requests/request-3:decide', expect.objectContaining({ approve: true }))
  expect(message.success).not.toHaveBeenCalled()
  expect(message.warning).toHaveBeenCalled()
  await waitFor(() => expect((screen.getAllByRole('checkbox')[2] as HTMLInputElement).checked).toBe(true))
  expect((screen.getAllByRole('checkbox')[1] as HTMLInputElement).checked).toBe(false)
}, 15000)

it('continues an independent group after a network failure and keeps the uncertain group selected', async () => {
  api.post.mockImplementation(async path => {
    if (path.endsWith('batch-decide')) throw new Error('synthetic timeout')
    return { data: { status: 'approved' } }
  })
  await approveAll()
  expect(api.post).toHaveBeenCalledWith('/approval-requests/request-3:decide', expect.anything())
  expect(message.success).not.toHaveBeenCalled()
  expect(message.warning).toHaveBeenCalled()
  expect((screen.getAllByRole('checkbox')[1] as HTMLInputElement).checked).toBe(true)
  expect((screen.getAllByRole('checkbox')[2] as HTMLInputElement).checked).toBe(true)
  expect((screen.getAllByRole('checkbox')[3] as HTMLInputElement).checked).toBe(false)
}, 15000)
