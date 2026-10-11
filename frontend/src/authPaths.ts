export const isPublicPage = (path: string) => /^\/(?:confirm\/[^/]+|share\/research\/[^/]+|w\/[^/]+|invite\/[^/]+|status)\/?$/.test(path)
export const isPublicRequest = (url: string | undefined) => /^\/(?:api\/v1\/)?public\//.test(url || '')
export function safeReturnTo(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/'
  const url = new URL(value, location.origin)
  return url.origin === location.origin && url.pathname !== '/login' ? `${url.pathname}${url.search}${url.hash}` : '/'
}
