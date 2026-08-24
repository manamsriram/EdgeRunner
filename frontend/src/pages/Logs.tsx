import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getLogs, type JournalUnit } from '../lib/api'

const UNITS: { value: JournalUnit; label: string; hint: string }[] = [
  { value: 'edgerunner', label: 'Trader', hint: 'app: ticks, tracebacks, OOM, restarts' },
  { value: 'edgerunner-deploy', label: 'Deploys', hint: 'the origin/live poller' },
  { value: 'caddy', label: 'Proxy', hint: 'TLS + HTTP' },
]

const RANGES = [
  { value: '', label: 'Latest' },
  { value: '1 hour ago', label: 'Last hour' },
  { value: '1 day ago', label: 'Last 24h' },
  { value: '7 days ago', label: 'Last 7 days' },
]

// journalctl -o short-iso lines start "2026-08-23T17:04:11-0700 host unit[pid]: msg".
// Colouring on the message body alone avoids matching the hostname or a pid.
function lineClass(line: string): string {
  const body = line.slice(line.indexOf(': ') + 2)
  if (/\b(ERROR|CRITICAL|Traceback|FAILED|!!)\b/.test(body)) return 'text-red-400'
  if (/\bWARNING\b/.test(body)) return 'text-amber-400'
  if (/\b(==>|Started|Deactivated)\b/.test(body)) return 'text-emerald-400'
  return 'text-zinc-400'
}

export default function Logs() {
  const [unit, setUnit] = useState<JournalUnit>('edgerunner')
  const [since, setSince] = useState('')
  const [lines, setLines] = useState(200)
  const [follow, setFollow] = useState(true)
  const [filter, setFilter] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, isError, error, dataUpdatedAt } = useQuery({
    queryKey: ['logs', unit, since, lines],
    queryFn: () => getLogs(unit, lines, since || undefined).then((r) => r.data),
    // Polling, not streaming: journalctl -f would hold a request open per viewer,
    // and this box runs a single uvicorn worker.
    refetchInterval: follow ? 10_000 : false,
  })

  const shown = (data?.lines ?? []).filter(
    (l) => !filter || l.toLowerCase().includes(filter.toLowerCase()),
  )

  useEffect(() => {
    if (follow) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [dataUpdatedAt, follow, shown.length])

  const status = (error as { response?: { status?: number } } | null)?.response?.status

  return (
    <div className="p-6 max-w-[1400px]">
      <div className="flex items-center gap-3 mb-1">
        <h1 className="text-xl font-bold text-white">Logs</h1>
        <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-amber-600/20 text-amber-400 border border-amber-600/40">
          REAL MONEY
        </span>
      </div>
      <p className="text-zinc-500 text-sm mb-5">
        systemd journal from the live VM. {UNITS.find((u) => u.value === unit)?.hint}
      </p>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="flex rounded-md overflow-hidden border border-zinc-800">
          {UNITS.map((u) => (
            <button
              key={u.value}
              onClick={() => setUnit(u.value)}
              className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                unit === u.value
                  ? 'bg-amber-600 text-white'
                  : 'bg-zinc-900 text-zinc-400 hover:bg-zinc-800'
              }`}
            >
              {u.label}
            </button>
          ))}
        </div>

        <select
          value={since}
          onChange={(e) => setSince(e.target.value)}
          className="bg-zinc-900 border border-zinc-800 text-zinc-300 text-sm rounded-md px-2 py-1.5"
        >
          {RANGES.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>

        <select
          value={lines}
          onChange={(e) => setLines(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 text-zinc-300 text-sm rounded-md px-2 py-1.5"
        >
          {[100, 200, 500, 1000, 2000].map((n) => (
            <option key={n} value={n}>
              {n} lines
            </option>
          ))}
        </select>

        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter…"
          className="bg-zinc-900 border border-zinc-800 text-zinc-300 text-sm rounded-md px-3 py-1.5 w-48 placeholder:text-zinc-600"
        />

        <label className="flex items-center gap-1.5 text-sm text-zinc-400 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
            className="accent-amber-600"
          />
          Follow
        </label>

        <span className="text-zinc-600 text-xs ml-auto">
          {shown.length}
          {filter && ` / ${data?.lines.length ?? 0}`} lines
          {dataUpdatedAt ? ` · ${new Date(dataUpdatedAt).toLocaleTimeString()}` : ''}
        </span>
      </div>

      <div className="bg-zinc-950 border border-zinc-800 rounded-lg h-[calc(100vh-230px)] overflow-auto">
        {isLoading && <div className="p-4 text-zinc-500 text-sm">Loading…</div>}

        {isError && (
          <div className="p-4 text-sm text-red-400">
            {status === 503
              ? 'No systemd journal on this host. Expected if VITE_LIVE_API_URL still points at Render — only the GCP VM has one.'
              : status === 401
                ? 'Session expired. Reload to sign in again.'
                : 'Could not read the journal. The service may be down; SSH is the fallback.'}
          </div>
        )}

        {!isLoading && !isError && shown.length === 0 && (
          <div className="p-4 text-zinc-500 text-sm">
            {filter ? 'No lines match the filter.' : 'No log lines in this range.'}
          </div>
        )}

        <pre className="text-[12px] leading-[1.5] font-mono p-3 whitespace-pre-wrap break-all">
          {shown.map((line, i) => (
            <div key={i} className={lineClass(line)}>
              {line}
            </div>
          ))}
        </pre>
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
