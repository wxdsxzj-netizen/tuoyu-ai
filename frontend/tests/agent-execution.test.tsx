import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import AgentPage from '../src/pages/Agent'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true, me: { tenant: { id: 't' }, user: { id: 'u', role_code: 'owner' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (s: string) => s }) }))
const task = { id: 'task', input_text: 'Queued work', mode: 'task', status: 'queued', plan: [] }
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(e => style(e))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear()
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/agent/tasks' ? { items: [task], total: 1 } : task }))
})
afterEach(cleanup)
it('refreshes queued progress and stops polling after completion', async () => {
  render(<MemoryRouter><AgentPage /></MemoryRouter>)
  fireEvent.click(await screen.findByText('Queued work'))
  await screen.findByText('p5.pause')
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/agent/tasks' ? { items: [task], total: 1 } : { ...task, status: 'completed' } }))
  await waitFor(() => expect(screen.queryByText('p5.pause')).toBeNull(), { timeout: 5000 })
  const count = api.get.mock.calls.filter(([url]) => url === '/agent/tasks/task').length
  await new Promise(resolve => setTimeout(resolve, 2200))
  expect(api.get.mock.calls.filter(([url]) => url === '/agent/tasks/task')).toHaveLength(count)
}, 15000)
it('blocks unknown-result resume and requires evidence for an owner retry', async () => {
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/agent/tasks' ? { items: [task], total: 1 }
    : { ...task, status: 'paused', pause_reason: 'outcome_unknown' } }))
  api.post.mockResolvedValue({ data: { ...task, status: 'completed' } })
  render(<MemoryRouter><AgentPage /></MemoryRouter>)
  fireEvent.click(await screen.findByText('Queued work'))
  await screen.findByText(/执行结果尚未确认/)
  expect(screen.queryByText('p5.resume')).toBeNull()
  const confirm = screen.getByRole('button', { name: '确认未执行并重试' }) as HTMLButtonElement
  expect(confirm.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('供应商未执行证据'), { target: { value: 'Provider receipt confirms no operation was applied.' } })
  fireEvent.click(confirm)
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/agent/tasks/task:reconcile-not-applied', { evidence: 'Provider receipt confirms no operation was applied.' }))
}, 15000)
