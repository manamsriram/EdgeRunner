import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getCalendar } from '../lib/api'
import type { CalendarDay, CalendarTrade } from '../lib/api'

const DAYS_OF_WEEK = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function fmt(n: number, decimals = 2): string {
  return n.toFixed(decimals)
}

function fmtPct(v: number): string {
  return (v >= 0 ? '+' : '') + fmt(v * 100) + '%'
}

function fmtAmt(v: number): string {
  return (v >= 0 ? '+$' : '-$') + fmt(Math.abs(v))
}

function DayPanel({ day, onClose }: { day: CalendarDay; onClose: () => void }) {
  const hasPnl = day.pnl_pct !== null
  return (
    <div
      className="fixed inset-0 z-40 flex justify-end"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-xl bg-zinc-900 border-l border-zinc-800 h-full overflow-auto p-6 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-zinc-500 hover:text-zinc-100 text-xl leading-none transition-colors"
        >
          ✕
        </button>

        <div className="mb-5">
          <div className="text-zinc-500 text-sm">{day.date}</div>
          <div className="text-2xl font-bold text-white mt-1">
            {hasPnl ? (
              <span className={day.pnl_pct! >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {fmtPct(day.pnl_pct!)}
              </span>
            ) : (
              <span className="text-zinc-600">No equity data</span>
            )}
            {hasPnl && (
              <span className="ml-3 text-lg font-normal text-zinc-400">
                {fmtAmt(day.pnl_amount!)}
              </span>
            )}
          </div>
        </div>

        <div className="text-zinc-300 text-sm font-semibold mb-3">
          Closed Trades ({day.trades.length})
        </div>

        {day.trades.length === 0 ? (
          <div className="text-zinc-600 text-sm">No closed trades this day.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead>
                <tr className="text-zinc-500 border-b border-zinc-800">
                  <th className="pb-2 pr-3">Symbol</th>
                  <th className="pb-2 pr-3">Strategy</th>
                  <th className="pb-2 pr-3">Opened</th>
                  <th className="pb-2 pr-3 text-right">Entry</th>
                  <th className="pb-2 pr-3 text-right">Exit</th>
                  <th className="pb-2 pr-3 text-right">Qty</th>
                  <th className="pb-2 pr-3 text-right">P&L%</th>
                  <th className="pb-2 text-right">P&L$</th>
                </tr>
              </thead>
              <tbody>
                {day.trades.map((t: CalendarTrade, i: number) => (
                  <tr key={i} className="border-b border-zinc-800/60">
                    <td className="py-2 pr-3 font-medium text-white">{t.symbol}</td>
                    <td className="py-2 pr-3 text-zinc-500">{t.strategy ?? 'Unknown'}</td>
                    <td className="py-2 pr-3 text-zinc-500">{t.open_date}</td>
                    <td className="py-2 pr-3 text-right text-zinc-300">${fmt(t.open_price)}</td>
                    <td className="py-2 pr-3 text-right text-zinc-300">${fmt(t.close_price)}</td>
                    <td className="py-2 pr-3 text-right text-zinc-300">{fmt(t.qty, 4)}</td>
                    <td className={`py-2 pr-3 text-right font-medium ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {fmtPct(t.pnl_pct)}
                    </td>
                    <td className={`py-2 text-right font-medium ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.pnl >= 0 ? '+' : ''}{fmt(t.pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

export default function Calendar() {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth()) // 0-indexed
  const [selectedDay, setSelectedDay] = useState<CalendarDay | null>(null)

  const { data = [], isLoading } = useQuery({
    queryKey: ['calendar'],
    queryFn: () => getCalendar().then(r => r.data),
    staleTime: 60_000,
  })

  const dayMap = new Map<string, CalendarDay>()
  for (const d of data) dayMap.set(d.date, d)

  const firstDay = new Date(year, month, 1).getDay() // 0=Sun
  const daysInMonth = new Date(year, month + 1, 0).getDate()

  function prevMonth() {
    if (month === 0) { setMonth(11); setYear(y => y - 1) }
    else setMonth(m => m - 1)
  }
  function nextMonth() {
    if (month === 11) { setMonth(0); setYear(y => y + 1) }
    else setMonth(m => m + 1)
  }

  const cells: Array<{ dateStr: string | null; dayData: CalendarDay | null }> = []
  for (let i = 0; i < firstDay; i++) cells.push({ dateStr: null, dayData: null })
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    cells.push({ dateStr, dayData: dayMap.get(dateStr) ?? null })
  }

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-white tracking-tight">P&L Calendar</h1>
        <div className="flex items-center gap-3">
          <button onClick={prevMonth} className="text-zinc-500 hover:text-zinc-100 w-8 h-8 flex items-center justify-center rounded-md hover:bg-zinc-800 transition-colors">‹</button>
          <span className="text-zinc-200 font-semibold w-40 text-center">{MONTHS[month]} {year}</span>
          <button onClick={nextMonth} className="text-zinc-500 hover:text-zinc-100 w-8 h-8 flex items-center justify-center rounded-md hover:bg-zinc-800 transition-colors">›</button>
        </div>
      </div>

      {isLoading ? (
        <div className="text-zinc-600 text-center py-20">Loading...</div>
      ) : (
        <div className="grid grid-cols-7 gap-1">
          {DAYS_OF_WEEK.map(d => (
            <div key={d} className="text-center text-xs text-zinc-600 font-medium py-2 uppercase tracking-wider">{d}</div>
          ))}
          {cells.map((cell, i) => {
            if (!cell.dateStr) {
              return <div key={i} />
            }
            const { dayData } = cell
            const hasPnl = dayData && dayData.pnl_pct !== null
            const hasTrades = dayData && dayData.trades.length > 0
            const hasAnyData = hasPnl || hasTrades
            const pnl = hasPnl ? dayData!.pnl_pct! : null
            const isToday = cell.dateStr === todayStr

            let cellClass = 'rounded-lg border p-2 min-h-[80px] flex flex-col cursor-default select-none transition-colors'
            if (!hasAnyData) {
              cellClass += ' border-zinc-800/50 bg-zinc-900/30'
            } else if (pnl !== null && pnl > 0) {
              cellClass += ' border-emerald-800/50 bg-emerald-950/40 cursor-pointer hover:bg-emerald-950/70'
            } else if (pnl !== null && pnl < 0) {
              cellClass += ' border-red-900/50 bg-red-950/30 cursor-pointer hover:bg-red-950/50'
            } else {
              cellClass += ' border-zinc-700/50 bg-zinc-900/50 cursor-pointer hover:bg-zinc-800/50'
            }

            return (
              <div
                key={cell.dateStr}
                className={cellClass}
                onClick={() => hasAnyData && dayData && setSelectedDay(dayData)}
              >
                <div className={`text-xs font-medium mb-1 ${isToday ? 'text-emerald-400' : 'text-zinc-500'}`}>
                  {parseInt(cell.dateStr.slice(8))}
                </div>
                {hasPnl && (
                  <>
                    <div className={`text-xs font-bold ${pnl! >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {fmtPct(pnl!)}
                    </div>
                    <div className={`text-xs ${dayData!.pnl_amount! >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
                      {fmtAmt(dayData!.pnl_amount!)}
                    </div>
                  </>
                )}
                {!hasPnl && hasTrades && (
                  <div className="text-xs text-zinc-600 mt-1">{dayData!.trades.length} trade{dayData!.trades.length !== 1 ? 's' : ''}</div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {selectedDay && (
        <DayPanel day={selectedDay} onClose={() => setSelectedDay(null)} />
      )}
    </div>
  )
}
