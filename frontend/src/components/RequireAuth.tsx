import { useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import toast from 'react-hot-toast'
import { supabase } from '../lib/supabase'

export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [signingIn, setSigningIn] = useState(false)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      setLoading(false)
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_event, s) => setSession(s))
    return () => sub.subscription.unsubscribe()
  }, [])

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    setSigningIn(true)
    try {
      const { error } = await supabase.auth.signInWithPassword({ email, password })
      if (error) throw error
    } catch {
      toast.error('Sign in failed')
    } finally {
      setSigningIn(false)
    }
  }

  if (loading) return null

  if (!session) {
    return (
      <div className="max-w-sm mx-auto mt-24 bg-zinc-900 rounded-xl p-6 border border-zinc-800">
        <h2 className="text-xl font-bold text-white mb-4 tracking-tight">Sign in required</h2>
        <form onSubmit={handleSignIn} className="flex flex-col gap-3">
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="bg-zinc-800 text-zinc-100 rounded-lg px-3 py-2 border border-zinc-700 focus:outline-none focus:border-emerald-600"
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="bg-zinc-800 text-zinc-100 rounded-lg px-3 py-2 border border-zinc-700 focus:outline-none focus:border-emerald-600"
          />
          <button
            type="submit"
            disabled={signingIn}
            className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 text-white font-medium px-4 py-2 rounded-lg transition-colors"
          >
            {signingIn ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    )
  }

  return <>{children}</>
}
