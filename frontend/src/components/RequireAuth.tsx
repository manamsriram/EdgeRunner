import { useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import toast from 'react-hot-toast'
import { authConfigured, authHost, supabase } from '../lib/supabase'

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

  const [authError, setAuthError] = useState<string | null>(null)

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    setSigningIn(true)
    setAuthError(null)
    try {
      // Supabase distinguishes "Invalid login credentials" from "Email not
      // confirmed" from a transport failure; collapsing all three into one
      // message makes a misconfigured project indistinguishable from a typo.
      const { error } = await supabase.auth.signInWithPassword({ email, password })
      if (error) throw error
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Sign in failed'
      setAuthError(message)
      toast.error(message)
    } finally {
      setSigningIn(false)
    }
  }

  if (loading) return null

  if (!session) {
    return (
      <div className="max-w-sm mx-auto mt-24 bg-zinc-900 rounded-xl p-6 border border-zinc-800">
        <h2 className="text-xl font-bold text-white mb-1 tracking-tight">Sign in required</h2>
        {/* Which project this form authenticates against. A user created in the
            other Supabase project fails here as "Invalid login credentials",
            which reads like a typo rather than a wrong-project mistake. */}
        <p className="text-zinc-600 text-[11px] mb-4 break-all">
          {authHost ?? 'VITE_SUPABASE_URL not set'}
        </p>

        {!authConfigured && (
          <div className="mb-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-[12px] text-red-300">
            VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are missing from this build.
            Set them in Vercel and redeploy — sign-in cannot work until then.
          </div>
        )}

        {authError && (
          <div className="mb-3 rounded-lg border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-[12px] text-amber-300">
            {authError}
            {/^email not confirmed$/i.test(authError) && (
              <span className="block mt-1 text-amber-400/70">
                Supabase → Authentication → Users → your user → confirm the email.
                Creating a user by hand needs “Auto Confirm User” ticked.
              </span>
            )}
            {/invalid login credentials/i.test(authError) && (
              <span className="block mt-1 text-amber-400/70">
                Also check the user exists in this project — the URL above — and
                not the other one.
              </span>
            )}
          </div>
        )}

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
