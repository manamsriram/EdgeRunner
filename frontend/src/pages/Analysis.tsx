import { useRef, useState } from 'react'

export default function Analysis() {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const abortRef = useRef<AbortController | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim() || loading) return

    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setLoading(true)
    setResult('')
    setError('')

    try {
      const res = await fetch('/api/analysis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
        signal: ctrl.signal,
      })

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const payload = JSON.parse(line.slice(6))
            if (payload.error) {
              setError(payload.error)
            } else if (payload.chunk) {
              setResult(payload.chunk)
            }
          } catch {
            // ignore malformed SSE lines
          }
        }
      }
    } catch (err: unknown) {
      if ((err as Error).name !== 'AbortError') {
        setError('Analysis failed. Check server logs.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-xl font-bold text-white mb-1 tracking-tight">Stock Analysis</h2>
        <p className="text-zinc-500 text-sm">Invest at your own risk. Analyzes stocks via LLM agent.</p>
      </div>

      <form onSubmit={handleSubmit} className="flex gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="E.g. Should I buy NVDA this week?"
          disabled={loading}
          className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-2 text-white placeholder-zinc-600 focus:outline-none focus:border-emerald-600 disabled:opacity-50 transition-colors"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white font-medium px-5 py-2 rounded-lg transition-colors whitespace-nowrap"
        >
          {loading ? 'Analyzing...' : 'Analyze'}
        </button>
        {loading && (
          <button
            type="button"
            onClick={() => abortRef.current?.abort()}
            className="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 px-4 py-2 rounded-lg transition-colors"
          >
            Cancel
          </button>
        )}
      </form>

      {error && (
        <div className="bg-red-950 border border-red-900 rounded-xl p-4 text-red-400 text-sm">
          {error}
        </div>
      )}

      {result && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-5">
          <h3 className="text-white font-semibold mb-3 tracking-tight">Analysis Result</h3>
          <p className="text-zinc-300 whitespace-pre-wrap text-sm leading-relaxed">{result}</p>
        </div>
      )}
    </div>
  )
}
