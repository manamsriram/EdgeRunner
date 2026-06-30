import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { approveProposal, getProposals, rejectProposal, type Proposal } from '../lib/api'
import { useWebSocket } from '../hooks/useWebSocket'

export default function Approvals() {
  const qc = useQueryClient()
  const { data: proposals = [], isLoading } = useQuery({
    queryKey: ['proposals'],
    queryFn: () => getProposals().then((r) => r.data),
    refetchInterval: 30_000,
  })

  const [pending, setPending] = useState<Set<number>>(new Set())

  useWebSocket((msg) => {
    const m = msg as { event: string; data: Proposal }
    if (m.event === 'new_proposal') {
      toast('New trade proposal arrived', { icon: '📋' })
      qc.invalidateQueries({ queryKey: ['proposals'] })
    }
  })

  const setInFlight = (id: number, inFlight: boolean) =>
    setPending((prev) => {
      const next = new Set(prev)
      inFlight ? next.add(id) : next.delete(id)
      return next
    })

  const handleApprove = async (id: number) => {
    if (pending.has(id)) return
    setInFlight(id, true)
    try {
      await approveProposal(id)
      toast.success(`Proposal #${id} approved and submitted`)
      qc.invalidateQueries({ queryKey: ['proposals'] })
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail ?? 'Approval failed')
    } finally {
      setInFlight(id, false)
    }
  }

  const handleReject = async (id: number) => {
    if (pending.has(id)) return
    setInFlight(id, true)
    try {
      await rejectProposal(id)
      toast.success(`Proposal #${id} rejected`)
      qc.invalidateQueries({ queryKey: ['proposals'] })
    } catch {
      toast.error('Rejection failed')
    } finally {
      setInFlight(id, false)
    }
  }

  return (
    <div>
      <h2 className="text-xl font-bold text-white mb-4 tracking-tight">Pending Trade Approvals</h2>

      {isLoading && <p className="text-zinc-500">Loading proposals...</p>}

      {!isLoading && proposals.length === 0 && (
        <div className="bg-zinc-900 rounded-xl p-8 text-center text-zinc-500 border border-zinc-800">
          No pending proposals. The scheduler will create new ones when signals fire.
        </div>
      )}

      <div className="flex flex-col gap-3">
        {proposals.map((p) => (
          <div
            key={p.id}
            className="bg-zinc-900 rounded-xl p-5 border border-zinc-800 flex flex-col gap-3"
          >
            <div className="flex items-center justify-between">
              <div>
                <span className="text-white font-bold text-lg">{p.symbol}</span>
                <span
                  className={`ml-2 px-2 py-0.5 rounded text-xs font-semibold uppercase ${
                    p.side === 'buy'
                      ? 'bg-emerald-950 text-emerald-400'
                      : 'bg-red-950 text-red-400'
                  }`}
                >
                  {p.side}
                </span>
              </div>
              <span className="text-zinc-300 font-mono">${p.notional.toFixed(2)}</span>
            </div>

            <div className="text-zinc-500 text-sm font-mono">
              <span className="mr-4">Ref: ${p.ref_price.toFixed(2)}</span>
              <span>{new Date(p.created_at).toLocaleString()}</span>
            </div>

            {p.reason && (
              <p className="text-zinc-300 text-sm bg-zinc-800 rounded-lg px-3 py-2">
                {p.reason}
              </p>
            )}

            <div className="flex gap-3">
              <button
                onClick={() => handleApprove(p.id)}
                disabled={pending.has(p.id)}
                className="flex-1 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white font-medium py-2 rounded-lg transition-colors"
              >
                {pending.has(p.id) ? 'Processing...' : 'Approve'}
              </button>
              <button
                onClick={() => handleReject(p.id)}
                disabled={pending.has(p.id)}
                className="flex-1 bg-red-800 hover:bg-red-700 disabled:opacity-50 text-white font-medium py-2 rounded-lg transition-colors"
              >
                Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
