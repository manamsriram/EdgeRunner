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
      toast.success('Kill switch engaged — trading halted')
    } catch {
      toast.error('Failed to engage kill switch')
    }
  }

  const handleDisengage = async () => {
    try {
      await disengageKillSwitch()
      qc.invalidateQueries({ queryKey: ['kill-switch'] })
      toast.success('Kill switch disengaged — trading resumed')
    } catch {
      toast.error('Failed to disengage kill switch')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Kill Switch */}
      <section className="bg-slate-800 rounded-xl p-6 border border-slate-700">
        <h2 className="text-xl font-bold text-white mb-4">Kill Switch</h2>
        <div className="flex items-center gap-4 mb-4">
          <div
            className={`w-3 h-3 rounded-full ${engaged ? 'bg-red-500' : 'bg-green-500'}`}
          />
          <span className={`font-bold text-lg ${engaged ? 'text-red-400' : 'text-green-400'}`}>
            {engaged ? 'ENGAGED — Trading Halted' : 'Disengaged — Trading Active'}
          </span>
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleEngage}
            disabled={engaged}
            className="bg-red-700 hover:bg-red-800 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Engage
          </button>
          <button
            onClick={handleDisengage}
            disabled={!engaged}
            className="bg-slate-600 hover:bg-slate-500 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Disengage
          </button>
        </div>
      </section>

      {/* Autonomy */}
      <section className="bg-slate-800 rounded-xl p-6 border border-slate-700">
        <h2 className="text-xl font-bold text-white mb-4">Autonomy Mode</h2>
        <div className="flex items-center gap-4 mb-4">
          <div
            className={`w-3 h-3 rounded-full ${currentMode === 'auto' ? 'bg-yellow-400' : 'bg-blue-400'}`}
          />
          <span className={`font-bold text-lg ${currentMode === 'auto' ? 'text-yellow-300' : 'text-blue-300'}`}>
            {currentMode === 'auto' ? 'AUTO — Trades execute automatically' : 'MANUAL — Proposals require approval'}
          </span>
        </div>
        <div className="flex gap-3 mb-3">
          <button
            onClick={() => handleSetAutonomy('manual')}
            disabled={currentMode === 'manual'}
            className="bg-blue-700 hover:bg-blue-800 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Manual
          </button>
          <button
            onClick={() => handleSetAutonomy('auto')}
            disabled={currentMode === 'auto'}
            className="bg-yellow-700 hover:bg-yellow-800 disabled:opacity-40 text-white font-medium px-5 py-2 rounded-lg transition-colors"
          >
            Auto
          </button>
        </div>
        <p className="text-slate-500 text-xs">
          Runtime only — resets to <code className="text-slate-400">AUTONOMY</code> env var on restart.
        </p>
      </section>

      {/* Run Log */}
      <section>
        <h2 className="text-xl font-bold text-white mb-3">Run Log (last 20)</h2>
        {runs.length === 0 ? (
          <div className="bg-slate-800 rounded-xl p-6 text-slate-400 border border-slate-700">
            No pipeline runs recorded yet.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-slate-700">
            <table className="w-full text-sm text-slate-300">
              <thead className="bg-slate-700 text-slate-400 uppercase text-xs">
                <tr>
                  {['#', 'Started', 'Strategy', 'Mode', 'Note'].map((h) => (
                    <th key={h} className="px-4 py-3 text-left">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="border-t border-slate-700">
                    <td className="px-4 py-3 text-slate-500">{r.id}</td>
                    <td className="px-4 py-3 text-slate-400">{new Date(r.started_at).toLocaleString()}</td>
                    <td className="px-4 py-3">{r.strategy}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-xs font-semibold uppercase ${
                        r.mode === 'auto' ? 'bg-yellow-900 text-yellow-300' : 'bg-blue-900 text-blue-300'
                      }`}>
                        {r.mode}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-400">{r.note ?? '—'}</td>
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
