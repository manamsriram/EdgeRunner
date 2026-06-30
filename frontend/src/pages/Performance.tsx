import { useQuery } from '@tanstack/react-query'
import { getPerformance } from '../lib/api'

const THRESHOLDS = {
  sharpe:        { min: 1.0,   label: '>=1.0'  },
  max_drawdown:  { max: -0.15, label: '<=15%'  },
  win_rate:      { min: 0.45,  label: '>=45%'  },
  profit_factor: { min: 1.5,   label: '>=1.5'  },
  trade_count:   { min: 100,   label: '>=100'  },
  days_active:   { min: 60,    label: '>=60'   },
}

function passes(metric: keyof typeof THRESHOLDS, value: number | null): boolean | null {
  if (value === null) return null
  const t = THRESHOLDS[metric]
  if ('min' in t) return value >= t.min
  if ('max' in t) return value >= t.max
  return null
}

function MetricTile({
  label,
  value,
  threshold,
  passing,
}: {
  label: string
  value: string
  threshold: string
  passing: boolean | null
}) {
  const border =
    passing === null ? 'border-zinc-700' : passing ? 'border-emerald-600' : 'border-red-600'
  const mark = passing === null ? '' : passing ? '✓' : '✗'
  const markColor = passing ? 'text-emerald-400' : 'text-red-400'

  return (
    <div className={`bg-zinc-900 rounded-xl p-4 border-2 ${border} flex flex-col gap-1`}>
      <div className="text-zinc-500 text-xs uppercase tracking-wider">{label}</div>
      <div className="text-white text-2xl font-bold font-mono">{value}</div>
      <div className="flex items-center gap-1 text-xs">
        <span className="text-zinc-600">{threshold}</span>
        {mark && <span className={`font-bold ${markColor}`}>{mark}</span>}
      </div>
    </div>
  )
}

function VerdictBanner({ verdict }: { verdict: string }) {
  const styles: Record<string, string> = {
    PASS: 'bg-emerald-950 border-emerald-600 text-emerald-300',
    FAIL: 'bg-red-950 border-red-700 text-red-300',
    INSUFFICIENT_DATA: 'bg-amber-950 border-amber-700 text-amber-300',
  }
  const labels: Record<string, string> = {
    PASS: '✓ GO-LIVE VERDICT: PASS',
    FAIL: '✗ GO-LIVE VERDICT: FAIL',
    INSUFFICIENT_DATA: '⚠ INSUFFICIENT DATA',
  }
  return (
    <div className={`rounded-xl border-2 px-6 py-4 font-bold text-lg tracking-tight ${styles[verdict] ?? styles.INSUFFICIENT_DATA}`}>
      {labels[verdict] ?? verdict}
    </div>
  )
}

function BenchmarkRow({ label, value }: { label: string; value: number | null }) {
  if (value === null)
    return (
      <div className="flex items-center justify-between py-2 border-t border-zinc-800">
        <span className="text-zinc-500 text-sm">{label}</span>
        <span className="text-zinc-600 text-sm font-mono">unavailable</span>
      </div>
    )
  const color = value >= 0 ? 'text-emerald-400' : 'text-red-400'
  return (
    <div className="flex items-center justify-between py-2 border-t border-zinc-800">
      <span className="text-zinc-300 text-sm">{label}</span>
      <span className={`font-mono font-bold text-sm ${color}`}>
        {value >= 0 ? '+' : ''}
        {(value * 100).toFixed(1)}%
      </span>
    </div>
  )
}

export default function Performance() {
  const { data: m, isLoading } = useQuery({
    queryKey: ['performance'],
    queryFn: () => getPerformance().then((r) => r.data),
    refetchInterval: 300_000,
  })

  if (isLoading) {
    return <p className="text-zinc-500">Loading performance data...</p>
  }

  if (!m || m.verdict === 'INSUFFICIENT_DATA') {
    return (
      <div className="flex flex-col gap-6">
        <h1 className="text-2xl font-bold text-white tracking-tight">Performance</h1>
        <div className="bg-zinc-900 rounded-xl p-6 text-zinc-500 border border-zinc-800">
          Not enough paper trading data yet - run the scheduler in auto mode to populate.
        </div>
      </div>
    )
  }

  const pfDisplay = m.profit_factor === null ? '∞' : m.profit_factor.toFixed(2)
  const pfPasses = m.profit_factor === null ? true : m.profit_factor >= THRESHOLDS.profit_factor.min

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-bold text-white tracking-tight">Performance</h1>

      <VerdictBanner verdict={m.verdict} />

      {m.failing_checks.length > 0 && (
        <div className="bg-red-950 border border-red-900 rounded-xl px-4 py-3">
          <div className="text-red-400 text-sm font-semibold mb-1">Failing checks:</div>
          <ul className="list-disc list-inside text-red-500 text-sm space-y-0.5">
            {m.failing_checks.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Metric tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricTile
          label="Sharpe"
          value={m.sharpe.toFixed(2)}
          threshold={THRESHOLDS.sharpe.label}
          passing={passes('sharpe', m.sharpe)}
        />
        <MetricTile
          label="Max Drawdown"
          value={`${(Math.abs(m.max_drawdown) * 100).toFixed(1)}%`}
          threshold={THRESHOLDS.max_drawdown.label}
          passing={passes('max_drawdown', m.max_drawdown)}
        />
        <MetricTile
          label="Win Rate"
          value={`${(m.win_rate * 100).toFixed(1)}%`}
          threshold={THRESHOLDS.win_rate.label}
          passing={passes('win_rate', m.win_rate)}
        />
        <MetricTile
          label="Profit Factor"
          value={pfDisplay}
          threshold={THRESHOLDS.profit_factor.label}
          passing={pfPasses}
        />
        <MetricTile
          label="Trades"
          value={String(m.trade_count)}
          threshold={THRESHOLDS.trade_count.label}
          passing={passes('trade_count', m.trade_count)}
        />
        <MetricTile
          label="Days Active"
          value={String(m.days_active)}
          threshold={THRESHOLDS.days_active.label}
          passing={passes('days_active', m.days_active)}
        />
      </div>

      {/* Benchmark */}
      <section>
        <h2 className="text-lg font-bold text-white mb-3 tracking-tight">
          Benchmark Comparison
          <span className="ml-2 text-xs font-normal text-zinc-600">(informational - not gated)</span>
        </h2>
        <div className="bg-zinc-900 rounded-xl px-4 border border-zinc-800">
          <BenchmarkRow label="Portfolio" value={m.total_return} />
          <BenchmarkRow label="SPY" value={m.benchmark_spy_return} />
        </div>
      </section>
    </div>
  )
}
