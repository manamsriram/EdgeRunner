import { useEffect, useRef } from 'react'
import { supabase } from '../lib/supabase'

async function wsUrl(): Promise<string> {
  // Only Approvals consumes this, and approvals exist only on the real-money
  // deployment (paper runs AUTONOMY=auto and never queues a proposal). Falls
  // back to the paper origin so local dev with one backend still works.
  const apiUrl = import.meta.env.VITE_LIVE_API_URL ?? import.meta.env.VITE_API_URL
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  if (apiUrl) {
    // The WS endpoint lives on the backend origin, not location.host (that's Vercel).
    return apiUrl.replace(/^http/, 'ws') + `/ws/updates${query}`
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/ws/updates${query}`
}

export function useWebSocket(onMessage: (msg: unknown) => void) {
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  useEffect(() => {
    let ws: WebSocket
    let retryDelay = 1000
    let stopped = false

    const connect = async () => {
      if (stopped) return
      const url = await wsUrl()
      if (stopped) return
      ws = new WebSocket(url)

      ws.onmessage = (e) => {
        try {
          onMessageRef.current(JSON.parse(e.data))
        } catch {
          // ignore malformed messages
        }
      }
      ws.onopen = () => {
        retryDelay = 1000
      }
      ws.onclose = () => {
        if (!stopped) {
          setTimeout(connect, retryDelay)
          retryDelay = Math.min(retryDelay * 2, 30_000)
        }
      }
      ws.onerror = () => ws.close()
    }

    connect()
    return () => {
      stopped = true
      ws?.close()
    }
  }, [])
}
