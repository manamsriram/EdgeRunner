import { useQuery, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import {
  disengageKillSwitch,
  engageKillSwitch,
  getAutonomy,
  getKillSwitch,
  getRuns,
  setAutonomy,
} from '../lib/api'

export default function Controls() {
  const qc = useQueryClient()

  const { data: ks } = useQuery({
    queryKey: ['kill-switch'],
    queryFn: () => getKillSwitch().then((r) => r.data),
    refetchInterval: 10_000,
  })

  const { data: autonomy } = useQuery({
    queryKey: ['autonomy'],
    queryFn: () => getAutonomy().then((r) => r.data),
  })

  const { data: runs = [] } = useQuery({
    queryKey: ['runs'],
    queryFn: () => getRuns().then((r) => r.data),
    refetchInterval: 30_000,
  })

  const engaged = ks?.engaged ?? false
  const currentMode = autonomy?.mode ?? 'manual'

  const handleSetAutonomy = async (mode: 'manual' | 'auto') => {
    try {
      await setAutonomy(mode)
      qc.invalidateQueries({ queryKey: ['autonomy'] })
      toast.success(`Autonomy set to ${mode}`)
    } catch {
      toast.error('Failed to update autonomy mode')
    }
  }

  const handleEngage = async () => {
    try {
      await engageKillSwitch()
      qc.invalidateQueries({ queryKey: ['kill-switch'] })
      toast.success('Kill switch engaged - trading halted')
    } catch {
      toast.error('Failed to engage kill switch')
    }
  }

  const handleDisengage = async () => {
    try {
      await disengageKillSwitch()
      qc.invalidateQueries({ queryKey: ['kill-switch'] })
      toast.success('Kill switch disengaged - trading resumed')
    } catch {
      toast.error('Failed to disengage kill switch')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Kill Switch */}
      <section className="bg-zinc-900 rounded-xl p-6 border border-zinc-800">
        <h2 className="text-xl font-bold text-white mb-4 tracking-tight">Kill Switch</h2>
        <div className="flex items-center gap-3 mb-5">
          <div
            className={`w-2.5 h-2.5 rounded-full ${engaged ? 'bg-red-500' : 'bg-emerald-500'}`}
          />
          <span className={`font-semibold ${engaged ? 'text-red-400' : 'text-emerald-400'}`}>
            {engaged ? 'ENGAGED - Trading Halted' : 'Disengaged - Trading Active'}
          </span>
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleEngage}
            disabled={engaged}
            className="bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Engage
          </button>
          <button
            onClick={handleDisengage}
            disabled={!engaged}
            className="bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Disengage
          </button>
        </div>
      </section>

      {/* Autonomy */}
      <section className="bg-zinc-900 rounded-xl p-6 border border-zinc-800">
        <h2 className="text-xl font-bold text-white mb-4 tracking-tight">Autonomy Mode</h2>
        <div className="flex items-center gap-3 mb-5">
          <div
            className={`w-2.5 h-2.5 rounded-full ${currentMode === 'auto' ? 'bg-amber-400' : 'bg-sky-400'}`}
          />
          <span className={`font-semibold ${currentMode === 'auto' ? 'text-amber-300' : 'text-sky-300'}`}>
            {currentMode === 'auto' ? 'AUTO - Trades execute automatically' : 'MANUAL - Proposals require approval'}
          </span>
        </div>
        <div className="flex gap-3 mb-3">
          <button
            onClick={() => handleSetAutonomy('manual')}
            disabled={currentMode === 'manual'}
            className="bg-sky-700 hover:bg-sky-600 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Manual
          </button>
          <button
            onClick={() => handleSetAutonomy('auto')}
            disabled={currentMode === 'auto'}
            className="bg-amber-700 hover:bg-amber-600 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Auto
          </button>
        </div>
        <p className="text-zinc-600 text-xs">
          Runtime only - resets to <code className="text-zinc-500">AUTONOMY</code> env var on restart.
        </p>
      </section>

      {/* Run Log */}
      <section>
        <h2 className="text-xl font-bold text-white mb-3 tracking-tight">Run Log (last 20)</h2>
        {runs.length === 0 ? (
          <div className="bg-zinc-900 rounded-xl p-6 text-zinc-500 border border-zinc-800">
            No pipeline runs recorded yet.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-zinc-800">
            <table className="w-full text-sm text-zinc-300">
              <thead className="bg-zinc-800 text-zinc-500 uppercase text-xs">
                <tr>
                  {['#', 'Started', 'Strategy', 'Mode', 'Note'].map((h) => (
                    <th key={h} className="px-4 py-3 text-left font-medium tracking-wider">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="border-t border-zinc-800 hover:bg-zinc-900/60 transition-colors">
                    <td className="px-4 py-3 text-zinc-600 font-mono text-xs">{r.id}</td>
                    <td className="px-4 py-3 text-zinc-500 font-mono text-xs">{new Date(r.started_at).toLocaleString()}</td>
                    <td className="px-4 py-3">{r.strategy}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-xs font-semibold uppercase ${
                        r.mode === 'auto' ? 'bg-amber-950 text-amber-400' : 'bg-sky-950 text-sky-400'
                      }`}>
                        {r.mode}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-zinc-500">{r.note ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
