import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import ImportsPage from '../src/pages/Imports'
import KbPage from '../src/pages/Kb'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/client', () => ({ api, TOKEN_KEY: 'test-token' }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => false, me: { tenant: { id: 't' }, user: { id: 'u' } } }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); vi.useFakeTimers() })
afterEach(() => { cleanup(); vi.useRealTimers() })

it.each([['/imports', ImportsPage], ['/kb/crawls', KbPage]] as const)('resumes polling running work on mounting %s and stops after completion', async (url, Page) => {
  let calls = 0
  api.get.mockImplementation((path: string) => {
    if (path === url) { calls++; return Promise.resolve({ data: [{ id: 'job', status: calls === 1 ? 'running' : 'completed', urls: [] }] }) }
    return Promise.resolve({ data: path === '/kb/items' ? { items: [], total: 0 } : {} })
  })
  await act(async () => { render(<Page />) })
  expect(calls).toBe(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
  expect(calls).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(calls).toBe(2)
}, 15000)

it('retries a failed status request without overlap and cancels polling on unmount', async () => {
  let calls = 0
  let resolve: (value: unknown) => void = () => {}
  api.get.mockImplementation((path: string) => {
    if (path !== '/imports') return Promise.resolve({ data: {} })
    calls++
    return calls === 1 ? Promise.reject(new Error('synthetic network outage')) : new Promise(r => { resolve = r })
  })
  const mounted = await act(async () => render(<ImportsPage />))
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(calls).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(2)
  mounted.unmount()
  await act(async () => { resolve({ data: [{ id: 'job', status: 'running' }] }); await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(2)
}, 15000)
