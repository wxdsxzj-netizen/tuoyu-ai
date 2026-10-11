import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import WidgetEmbedPage from '../src/pages/WidgetEmbed'
import ConfirmPage from '../src/pages/Confirm'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('axios', () => ({ default: http }))
const config = { kind: 'form', cookie_banner: false, fields: [{ key: 'email', label: 'Email', required: true },
  { key: 'message', label: 'Message', required: true }], consent_text: 'I agree' }
beforeAll(() => {
  const style = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => style(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
})
beforeEach(() => { vi.clearAllMocks(); localStorage.clear() })
afterEach(cleanup)
function widget() { return render(<MemoryRouter initialEntries={['/w/test']}><Routes><Route path="/w/:key" element={<WidgetEmbedPage />} /></Routes></MemoryRouter>) }

it('keeps the inquiry form and input after server failure and only shows success after retry', async () => {
  http.get.mockResolvedValue({ data: config })
  http.post.mockRejectedValueOnce({ response: { status: 500, data: { error: { message_en: 'Please retry' } } } })
  widget()
  fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'synthetic@example.com' } })
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Synthetic inquiry' } })
  fireEvent.click(screen.getByLabelText('I agree')); fireEvent.click(screen.getByText('Send'))
  await screen.findByText('Please retry')
  expect(screen.getByText('Please retry').closest('.ant-alert')?.classList.contains('ant-alert-error')).toBe(true)
  expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('Synthetic inquiry')
  http.post.mockResolvedValueOnce({ data: { message: 'Inquiry received' } })
  fireEvent.click(screen.getByText('Send'))
  await screen.findByText('Inquiry received')
  expect(screen.queryByLabelText('Message')).toBeNull()
})

it('retains the chat session token on a transient polling failure', async () => {
  localStorage.setItem('tl_chat_test', 'synthetic-session')
  http.get.mockImplementation((url: string) => url.includes('/config') ? Promise.resolve({ data: { ...config, kind: 'chat' } }) : Promise.reject(new Error('offline')))
  await act(async () => { widget() })
  await waitFor(() => expect(http.get).toHaveBeenCalledWith('/api/v1/public/chat/synthetic-session'))
  expect(localStorage.getItem('tl_chat_test')).toBe('synthetic-session')
  expect(screen.getByPlaceholderText('Type a message')).toBeTruthy()
})

it('preserves chat text until the server acknowledges it and blocks a second pending send', async () => {
  localStorage.setItem('tl_chat_test', 'synthetic-session')
  http.get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('/config') ? { ...config, kind: 'chat' } : { messages: [] } }))
  http.post.mockReturnValue(new Promise(() => {}))
  widget()
  fireEvent.change(await screen.findByPlaceholderText('Type a message'), { target: { value: 'Keep this text' } })
  fireEvent.click(screen.getByText('Send'))
  await waitFor(() => expect(http.post).toHaveBeenCalledTimes(1))
  expect((screen.getByPlaceholderText('Type a message') as HTMLInputElement).value).toBe('Keep this text')
  fireEvent.click(screen.getByText('Send'))
  expect(http.post).toHaveBeenCalledTimes(1)
})

it('prevents duplicate quotation confirmations while the request is pending', async () => {
  http.get.mockResolvedValue({ data: { quote_no: 'TEST', doc_version: 1, items: [], currency: 'USD', total: 100 } })
  http.post.mockReturnValue(new Promise(() => {}))
  render(<MemoryRouter initialEntries={['/confirm/test']}><Routes><Route path="/confirm/:token" element={<ConfirmPage />} /></Routes></MemoryRouter>)
  fireEvent.change(await screen.findByLabelText('Your name'), { target: { value: 'Synthetic Buyer' } })
  fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'synthetic@example.com' } })
  fireEvent.click(screen.getByText('Confirm quotation')); fireEvent.click(screen.getByText('Confirm quotation'))
  await act(async () => {})
  expect(http.post).toHaveBeenCalledTimes(1)
})

it('retains quotation input and offers retry after a rate limit response', async () => {
  http.get.mockResolvedValue({ data: { quote_no: 'TEST', doc_version: 1, items: [], currency: 'USD', total: 100 } })
  http.post.mockRejectedValueOnce({ response: { status: 429 } })
  render(<MemoryRouter initialEntries={['/confirm/test']}><Routes><Route path="/confirm/:token" element={<ConfirmPage />} /></Routes></MemoryRouter>)
  fireEvent.change(await screen.findByLabelText('Your name'), { target: { value: 'Synthetic Buyer' } })
  fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'synthetic@example.com' } })
  fireEvent.click(screen.getByText('Confirm quotation'))
  await screen.findByText('Unable to submit. Please retry or contact your sales representative.')
  expect((screen.getByLabelText('Your name') as HTMLInputElement).value).toBe('Synthetic Buyer')
  http.post.mockResolvedValueOnce({ data: { status: 'locked' } })
  fireEvent.click(screen.getByText('Confirm quotation'))
  await screen.findByText('Thank you, the quotation is confirmed.')
})

it('keeps failed chat text for retry and only clears a confirmed invalid session', async () => {
  localStorage.setItem('tl_chat_test', 'synthetic-session')
  http.get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('/config') ? { ...config, kind: 'chat' } : { messages: [] } }))
  http.post.mockRejectedValueOnce({ response: { status: 500 } })
  widget()
  fireEvent.change(await screen.findByPlaceholderText('Type a message'), { target: { value: 'Keep this text' } })
  fireEvent.click(screen.getByText('Send'))
  await screen.findByText('Failed, please retry')
  expect((screen.getByPlaceholderText('Type a message') as HTMLInputElement).value).toBe('Keep this text')
  cleanup()
  http.get.mockImplementation((url: string) => url.includes('/config') ? Promise.resolve({ data: { ...config, kind: 'chat' } }) : Promise.reject({ response: { status: 404 } }))
  widget()
  await screen.findByText('Chat with us')
  expect(localStorage.getItem('tl_chat_test')).toBeNull()
})
