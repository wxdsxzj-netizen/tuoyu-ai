import axios, { AxiosError } from 'axios'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../src/App'
import { AuthProvider } from '../src/auth'
import { api, TOKEN_KEY } from '../src/api/client'
import { safeReturnTo } from '../src/authPaths'

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it.each(['none', 'valid', 'expired'])('renders a public report without waiting for private auth with a %s token', async token => {
  if (token !== 'none') localStorage.setItem(TOKEN_KEY, `synthetic-${token}`)
  const privateGet = vi.spyOn(api, 'get').mockImplementation(() => new Promise(() => {}))
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { company: 'Synthetic public report', sections: [], evidence: [] } })
  render(<MemoryRouter initialEntries={['/share/research/test']}><AuthProvider><App /></AuthProvider></MemoryRouter>)
  await screen.findByText('Synthetic public report')
  expect(privateGet).not.toHaveBeenCalledWith('/auth/me')
}, 15000)

it('sends public API calls without a stale private bearer and preserves login state on public 401', async () => {
  localStorage.setItem(TOKEN_KEY, 'synthetic-expired')
  const original = api.defaults.adapter
  let authorization: unknown
  api.defaults.adapter = async cfg => {
    authorization = cfg.headers.Authorization
    throw new AxiosError('synthetic public session invalid', 'ERR_BAD_REQUEST', cfg, undefined,
      { status: 401, statusText: 'Unauthorized', config: cfg, headers: {}, data: { error: { code: 'PUBLIC_INVALID', message: 'invalid', trace_id: 'test' } } })
  }
  try {
    await expect(api.get('/public/invitations/test')).rejects.toBeTruthy()
    expect(authorization).toBeUndefined()
    expect(localStorage.getItem(TOKEN_KEY)).toBe('synthetic-expired')
  } finally { api.defaults.adapter = original }
})

function LocationState() { const loc = useLocation(); return <output data-testid="location">{JSON.stringify({ path: loc.pathname, search: loc.search, hash: loc.hash, state: loc.state })}</output> }
it('keeps the exact private destination while redirecting to login', async () => {
  render(<MemoryRouter initialEntries={['/inbox?c=synthetic#reply']}><AuthProvider><App /><LocationState /></AuthProvider></MemoryRouter>)
  await waitFor(() => expect(JSON.parse(screen.getByTestId('location').textContent!).path).toBe('/login'))
  expect(JSON.parse(screen.getByTestId('location').textContent!).state?.returnTo).toBe('/inbox?c=synthetic#reply')
}, 15000)

it('returns to the original private URL after successful login', async () => {
  vi.spyOn(api, 'post').mockResolvedValue({ data: { access_token: 'synthetic-token' } })
  vi.spyOn(api, 'get').mockImplementation(async path => ({ data: path === '/auth/me'
    ? { user: { id: 'u', name: 'Synthetic' }, tenant: { id: 't', name: 'Test' }, permissions: [] }
    : { items: [], unread: 0 } }))
  render(<MemoryRouter initialEntries={['/inbox?source=notification#reply']}><AuthProvider><App /><LocationState /></AuthProvider></MemoryRouter>)
  fireEvent.change(await screen.findByLabelText('邮箱'), { target: { value: 'synthetic@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'Synthetic-test-only' } })
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }))
  await waitFor(() => expect(JSON.parse(screen.getByTestId('location').textContent!).path).toBe('/inbox'))
  const loc = JSON.parse(screen.getByTestId('location').textContent!)
  expect(loc.search + loc.hash).toBe('?source=notification#reply')
}, 15000)

it('rejects external or looping return destinations', () => {
  for (const path of ['https://example.com', '//example.com', '/\\example.com', '/login', null]) expect(safeReturnTo(path)).toBe('/')
  expect(safeReturnTo('/inbox?c=test#reply')).toBe('/inbox?c=test#reply')
})

it('lands an existing overdue-order notification on the orders tab', async () => {
  localStorage.setItem(TOKEN_KEY, 'synthetic-token')
  const get = vi.spyOn(api, 'get').mockImplementation(async path => ({ data: path === '/auth/me'
    ? { user: { id: 'u', name: 'Synthetic' }, tenant: { id: 't', name: 'Test' }, permissions: [] }
    : path === '/orders' ? [] : { items: [], unread: 0 } }))
  render(<MemoryRouter initialEntries={['/crm?tab=orders']}><AuthProvider><App /><LocationState /></AuthProvider></MemoryRouter>)
  await waitFor(() => expect(get).toHaveBeenCalledWith('/orders'))
  expect(JSON.parse(screen.getByTestId('location').textContent!).path).toBe('/crm-plus')
  expect(get).not.toHaveBeenCalledWith('/samples')
}, 15000)
