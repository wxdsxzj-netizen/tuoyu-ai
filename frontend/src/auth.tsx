import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, TOKEN_KEY, type Me } from './api/client'

interface AuthCtx { me: Me | null; loading: boolean; can: (p: string) => boolean; setToken: (t: string) => Promise<void>; logout: () => void }
const Ctx = createContext<AuthCtx>(null as unknown as AuthCtx)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  const load = useCallback(async () => {
    if (!localStorage.getItem(TOKEN_KEY)) { setLoading(false); return }
    try { setMe((await api.get<Me>('/auth/me')).data) } catch { setMe(null) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  const setToken = async (t: string) => { localStorage.setItem(TOKEN_KEY, t); setLoading(true); await load() }
  const logout = () => { localStorage.removeItem(TOKEN_KEY); setMe(null) }
  const can = (p: string) => !!me?.permissions.includes(p)
  return <Ctx.Provider value={{ me, loading, can, setToken, logout }}>{children}</Ctx.Provider>
}
export const useAuth = () => useContext(Ctx)
