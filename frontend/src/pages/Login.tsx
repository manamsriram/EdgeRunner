import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { login } from '../lib/api'

export default function Login() {
  const [form, setForm] = useState({ username: '', password: '' })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }))

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(form.username, form.password)
      qc.invalidateQueries({ queryKey: ['me'] })
      navigate('/portfolio')
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Something went wrong'
      setError(Array.isArray(msg) ? msg.map((m: { msg: string }) => m.msg).join(', ') : msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-900 flex items-center justify-center p-4">
      <div className="bg-slate-800 rounded-xl p-8 w-full max-w-md border border-slate-700 shadow-xl">
        <h1 className="text-2xl font-bold text-white mb-6">Trading Agent Dashboard</h1>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <input
            placeholder="Username"
            value={form.username}
            onChange={set('username')}
            required
            className="bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 text-white placeholder-slate-400 focus:outline-none focus:border-blue-500"
          />
          <input
            placeholder="Password"
            type="password"
            value={form.password}
            onChange={set('password')}
            required
            className="bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 text-white placeholder-slate-400 focus:outline-none focus:border-blue-500"
          />

          {error && <p className="text-sm text-red-400">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-medium py-2 rounded-lg transition-colors"
          >
            {loading ? 'Please wait…' : 'Login'}
          </button>
        </form>
      </div>
    </div>
  )
}
