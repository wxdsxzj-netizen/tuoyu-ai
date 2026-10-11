import { webcrypto } from 'node:crypto'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import AgentPage from '../src/pages/Agent'
import { MemoryRouter } from 'react-router-dom'
import ImportsPage from '../src/pages/Imports'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api, TOKEN_KEY: 'test-token' }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true, me: { tenant: { id: 't' }, user: { id: 'u' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear(); vi.stubGlobal('crypto', webcrypto)
  api.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/agent/tasks' ? { items: [], balance: 100 }
    : url === '/imports/fields' ? { companies: [{ field: 'name', required: true }] } : [] }))
  api.post.mockReturnValue(new Promise(() => {}))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('locks Agent creation immediately and persists one request identity', async () => {
  render(<MemoryRouter><AgentPage /></MemoryRouter>)
  fireEvent.change(screen.getByPlaceholderText('p5.agentPlaceholder'), { target: { value: 'Synthetic agent task' } })
  fireEvent.click(screen.getByText('p5.send'))
  fireEvent.click(screen.getByText('p5.send'))
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
  expect(api.post.mock.calls[0][1].request_id).toMatch(/^[0-9a-f-]{36}$/)
  expect((screen.getByPlaceholderText('p5.agentPlaceholder') as HTMLTextAreaElement).disabled).toBe(true)
})

it('restores and safely retries an Agent creation after a network timeout', async () => {
  api.post.mockRejectedValueOnce(new Error('synthetic timeout'))
  render(<MemoryRouter><AgentPage /></MemoryRouter>)
  fireEvent.change(screen.getByPlaceholderText('p5.agentPlaceholder'), { target: { value: 'Synthetic agent task' } })
  fireEvent.click(screen.getByText('p5.send'))
  await screen.findByText('上次任务提交结果待确认，请重试原请求。')
  const original = api.post.mock.calls[0][1]
  cleanup(); render(<MemoryRouter><AgentPage /></MemoryRouter>)
  expect((screen.getByPlaceholderText('p5.agentPlaceholder') as HTMLTextAreaElement).value).toBe(original.text)
  api.post.mockResolvedValueOnce({ data: { id: 'task', mode: 'help', answer: 'Synthetic answer' } })
  fireEvent.click(screen.getByText('p5.send'))
  await screen.findByText('Synthetic answer')
  expect(api.post.mock.calls[1][1]).toEqual(original)
  expect(sessionStorage.length).toBe(0)
})

async function chooseImport() {
  const file = new File(['name,domain\nSynthetic,synthetic.example.com'], 'synthetic.csv', { type: 'text/csv' })
  Object.defineProperty(file, 'arrayBuffer', { value: () => Promise.resolve(new TextEncoder().encode('name,domain\nSynthetic,synthetic.example.com').buffer) })
  fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [file] } })
  await screen.findByText('import.start')
}

it('prevents duplicate import starts and reuses its multipart request identity after remount', async () => {
  let submitCount = 0
  api.post.mockImplementation((path: string) => {
    if (path === '/imports:preview') return Promise.resolve({ data: { headers: ['name', 'domain'], sample: [['Synthetic', 'synthetic.example.com']], total: 1, mapping: { name: 'name' }, missing_required: [] } })
    submitCount++
    return submitCount === 1 ? Promise.reject(new Error('synthetic timeout')) : Promise.resolve({ data: { id: 'import' } })
  })
  render(<ImportsPage />)
  await chooseImport()
  fireEvent.click(screen.getByText('import.start')); fireEvent.click(screen.getByText('import.start'))
  await screen.findByText('上次导入提交结果待确认，请重新选择原文件并重试。')
  const first = api.post.mock.calls.find(([path]) => path === '/imports')![1] as FormData
  expect(submitCount).toBe(1)
  expect(first.get('request_id')).toMatch(/^[0-9a-f-]{36}$/)
  cleanup(); render(<ImportsPage />)
  await chooseImport()
  fireEvent.click(screen.getByText('import.start'))
  await waitFor(() => expect(submitCount).toBe(2))
  const second = api.post.mock.calls.filter(([path]) => path === '/imports')[1][1] as FormData
  expect(second.get('request_id')).toBe(first.get('request_id'))
  await waitFor(() => expect(sessionStorage.length).toBe(0))
}, 15000)

it('consumes the support assistant prefill without automatically submitting it', () => {
  render(<MemoryRouter initialEntries={[{ pathname: '/agent', state: { prefill: 'Synthetic support task' } }]}><AgentPage /></MemoryRouter>)
  expect((screen.getByPlaceholderText('p5.agentPlaceholder') as HTMLTextAreaElement).value).toBe('Synthetic support task')
  expect(api.post).not.toHaveBeenCalled()
})
