import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import InboxPage from '../src/pages/Inbox'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }))
vi.mock('../src/api/client', () => ({ api }))
vi.mock('../src/auth', () => ({ useAuth: () => ({ can: () => true }) }))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
const conversation = (id: string) => ({ id, peer_email: `${id}@buyer.test`, subject: id, status: 'pending', language: 'en' })
const detail = (id: string) => ({ data: { conversation: conversation(id), messages: [{ id, direction: 'in',
  sent_by: 'customer', body: `message from ${id}`, created_at: '2026-01-01T00:00:00Z' }] } })
const checked = { data: { gate: { decision: 'allow', blocks: [] }, qa: { blocking: [], warnings: [] } } }

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => {
  vi.clearAllMocks()
  api.get.mockImplementation((path: string) => Promise.resolve(path === '/conversations'
    ? { data: { items: [conversation('A'), conversation('B')] } } : detail(path.split('/').pop()!)))
  api.post.mockResolvedValue(checked)
})
afterEach(cleanup)

async function select(id: string) {
  fireEvent.click((await screen.findAllByText(`${id}@buyer.test`))[0])
}
function replyBox() { return screen.getByPlaceholderText('p4.replyPh') as HTMLTextAreaElement }

describe('conversation identity across asynchronous inbox operations', () => {
  it('ignores a late A detail after selecting B and sends only the displayed B draft', async () => {
    const a = deferred<ReturnType<typeof detail>>()
    api.get.mockImplementation((path: string) => path === '/conversations/A' ? a.promise : Promise.resolve(
      path === '/conversations' ? { data: { items: [conversation('A'), conversation('B')] } } : detail('B')))
    render(<InboxPage />)
    await select('A')
    await select('B')
    await screen.findByText('message from B')
    await act(async () => a.resolve(detail('A')))
    expect(screen.queryByText('message from A')).toBeNull()
    expect(screen.getByText('message from B')).toBeTruthy()
    fireEvent.change(replyBox(), { target: { value: 'reply to B' } })
    fireEvent.click(screen.getByText('p4.precheck'))
    await waitFor(() => expect(screen.getByText('p4.send').closest('button')!.disabled).toBe(false))
    fireEvent.click(screen.getByText('p4.send'))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/conversations/B:reply', { body: 'reply to B' }))
  })

  it('clears A immediately while B is loading and ignores A suggestions after switching', async () => {
    const suggestion = deferred<{ data: { drafts: { body: string }[] } }>()
    const b = deferred<ReturnType<typeof detail>>()
    api.post.mockReturnValue(suggestion.promise)
    render(<InboxPage />)
    await select('A')
    await screen.findByText('message from A')
    fireEvent.click(screen.getByText('p4.aiDraft'))
    api.get.mockImplementation((path: string) => path === '/conversations/B' ? b.promise : Promise.resolve(detail('A')))
    await select('B')
    expect(screen.queryByText('message from A')).toBeNull()
    expect(screen.queryByPlaceholderText('p4.replyPh')).toBeNull()
    await act(async () => b.resolve(detail('B')))
    fireEvent.change(replyBox(), { target: { value: 'B draft' } })
    await act(async () => suggestion.resolve({ data: { drafts: [{ body: 'A suggestion' }] } }))
    expect(replyBox().value).toBe('B draft')
  })

  it('does not authorize a changed draft with an older precheck response', async () => {
    const check = deferred<typeof checked>()
    api.post.mockReturnValue(check.promise)
    render(<InboxPage />)
    await select('A')
    await screen.findByText('message from A')
    fireEvent.change(replyBox(), { target: { value: 'checked text' } })
    fireEvent.click(screen.getByText('p4.precheck'))
    fireEvent.change(replyBox(), { target: { value: 'changed text' } })
    await act(async () => check.resolve(checked))
    expect(screen.getByText('p4.send').closest('button')!.disabled).toBe(true)
  })

  it('keeps B selected and preserves its draft when a pending A send completes', async () => {
    const sent = deferred<{ data: object }>()
    api.post.mockImplementation((path: string) => path.endsWith(':reply') ? sent.promise : Promise.resolve(checked))
    render(<InboxPage />)
    await select('A')
    await screen.findByText('message from A')
    fireEvent.change(replyBox(), { target: { value: 'A reply' } })
    fireEvent.click(screen.getByText('p4.precheck'))
    await waitFor(() => expect(screen.getByText('p4.send').closest('button')!.disabled).toBe(false))
    fireEvent.click(screen.getByText('p4.send'))
    await select('B')
    await screen.findByText('message from B')
    fireEvent.change(replyBox(), { target: { value: 'B unsent draft' } })
    await act(async () => sent.resolve({ data: {} }))
    expect(screen.getByText('message from B')).toBeTruthy()
    expect(replyBox().value).toBe('B unsent draft')
    expect(api.post.mock.calls.filter(([path]) => path.endsWith(':reply'))).toEqual([
      ['/conversations/A:reply', { body: 'A reply' }],
    ])
  })
})
