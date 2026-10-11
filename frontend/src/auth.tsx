import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { api, TOKEN_KEY, type Me } from './api/client'
import { isPublicPage } from './authPaths'

interface AuthCtx { me: Me | null; loading: boolean; can: (p: string) => boolean; setToken: (t: string) => Promise<void>; logout: () => void }
const Ctx = createContext<AuthCtx>(null as unknown as AuthCtx)

export function AuthProvider({ children }: { children: ReactNode }) {
  const publicPage = isPublicPage(useLocation().pathname)
  const [me, setMe] = useState<Me | null>(null)
  const [pending, setLoading] = useState(true)
  const [resolvedScope, setResolvedScope] = useState(publicPage)
  const generation = useRef(0)
  const load = useCallback(async () => {
    const current = ++generation.current
    setLoading(true)
    try {
      if (publicPage) return
      if (!localStorage.getItem(TOKEN_KEY)) { setMe(null); return }
      const result = await api.get<Me>('/auth/me')
      if (current === generation.current) setMe(result.data)
    } catch { if (current === generation.current) setMe(null) }
    finally { if (current === generation.current) { setResolvedScope(publicPage); setLoading(false) } }
  }, [publicPage])
  const invalidate = useCallback(() => { generation.current++ }, [])
  useEffect(() => { load(); return invalidate }, [load, invalidate])
  const setToken = async (t: string) => { localStorage.setItem(TOKEN_KEY, t); setLoading(true); await load() }
  const logout = () => { generation.current++; localStorage.removeItem(TOKEN_KEY); setMe(null); setLoading(false) }
  const can = (p: string) => !!me?.permissions.includes(p)
  return <Ctx.Provider value={{ me, loading: pending || resolvedScope !== publicPage, can, setToken, logout }}>{children}</Ctx.Provider>
}
export const useAuth = () => useContext(Ctx)
