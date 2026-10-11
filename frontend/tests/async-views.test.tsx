import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { Modal } from 'antd'
import DataHubPage from '../src/pages/DataHub'
import ApprovalsPage from '../src/pages/Approvals'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => { vi.clearAllMocks(); api.get.mockResolvedValue({ data: { items: [] } }); api.post.mockResolvedValue({ data: {} }) })
afterEach(() => cleanup())
function deferred() { let resolve!: (x: unknown) => void; const promise = new Promise(r => { resolve = r }); return { resolve, promise } }

it('keeps peer results when the supplier-switch response arrives later', async () => {
  const sw = deferred()
  api.get.mockImplementation((url: string) => url === '/customs/supplier-switch' ? sw.promise : url === '/customs/peers'
    ? Promise.resolve({ data: { rivals: [{ exporter: 'Synthetic peer', shared_buyers: 1 }] } }) : new Promise(() => {}))
  render(<DataHubPage />)
  fireEvent.click(screen.getByRole('tab', { name: 'b6.tabCustoms' }))
  // The revised form requires explicit exporter input; the old form has none.
  const exporter = screen.queryByPlaceholderText('exporter domain')
  if (exporter) fireEvent.change(exporter, { target: { value: 'synthetic.example.com' } })
  fireEvent.click(screen.getByText('b6.analyze'))
  await screen.findByText('Synthetic peer')
  await act(async () => sw.resolve({ data: { switched: false } }))
  await screen.findByText('b6.noSwitch')
  expect(screen.queryByText('Synthetic peer')).not.toBeNull()
})

it('uses the entered exporter and ignores an older analysis response', async () => {
  const first = deferred(); const second = deferred()
  api.get.mockImplementation((url: string, cfg) => url === '/customs/peers' ? (cfg.params.exporter_domain === 'first.example.com' ? first.promise : second.promise) : new Promise(() => {}))
  render(<DataHubPage />)
  fireEvent.click(screen.getByRole('tab', { name: 'b6.tabCustoms' }))
  const exporter = screen.getByPlaceholderText('exporter domain')
  fireEvent.change(exporter, { target: { value: 'first.example.com' } }); fireEvent.click(screen.getByText('b6.analyze'))
  fireEvent.change(exporter, { target: { value: 'second.example.com' } }); fireEvent.click(screen.getByText('b6.analyze'))
  await act(async () => second.resolve({ data: { rivals: [{ exporter: 'Current peer' }] } }))
  await screen.findByText('Current peer')
  await act(async () => first.resolve({ data: { rivals: [{ exporter: 'Stale peer' }] } }))
  expect(screen.queryByText('Stale peer')).toBeNull()
  expect(screen.queryByText('Current peer')).not.toBeNull()
})

it('replaces the editor with the regenerated draft before approval', async () => {
  let regenerated = false
  const draft = { id: 'draft', subject: 'Old subject', body: 'Old body', qa_score: 90 }
  api.get.mockImplementation(async () => ({ data: { items: [{ ...draft, ...(regenerated ? { subject: 'New subject', body: 'New body' } : {}) }], total: 1, sequences_pending: [], enrollments_pending: 0 } }))
  api.post.mockImplementation(async path => { if (path.endsWith(':regenerate')) regenerated = true; return { data: {} } })
  render(<ApprovalsPage />)
  await screen.findByDisplayValue('Old body')
  fireEvent.click(screen.getByText('p3.regenerate'))
  await screen.findByDisplayValue('New body')
  expect(screen.queryByDisplayValue('Old body')).toBeNull()
  fireEvent.click(screen.getByText('p3.approve'))
  await waitFor(() => expect(api.post).toHaveBeenLastCalledWith('/approvals/draft:decide', { decision: 'approve' }))
})

it('keeps unsaved text when the user cancels regeneration', async () => {
  api.get.mockResolvedValue({ data: { items: [{ id: 'draft', subject: 'Subject', body: 'Old body', qa_score: 90 }], total: 1, sequences_pending: [], enrollments_pending: 0 } })
  let confirmation: Parameters<typeof Modal.confirm>[0] | undefined
  const confirm = vi.spyOn(Modal, 'confirm').mockImplementation(cfg => { confirmation = cfg; return { destroy: vi.fn(), update: vi.fn() } })
  try {
    render(<ApprovalsPage />)
    fireEvent.change(await screen.findByDisplayValue('Old body'), { target: { value: 'Unsaved edit' } })
    fireEvent.click(screen.getByText('p3.regenerate'))
    expect(confirmation?.content).toBe('当前未保存修改会被新草稿替换。')
    expect(api.post).not.toHaveBeenCalled()
    expect(screen.queryByDisplayValue('Unsaved edit')).not.toBeNull()
  } finally { confirm.mockRestore() }
})
