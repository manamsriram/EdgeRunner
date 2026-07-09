import { useEffect, useRef } from 'react'
import { getAuthToken } from '../lib/api'

function wsUrl(): string {
  const apiUrl = import.meta.env.VITE_API_URL
  const token = getAuthToken()
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  if (apiUrl) {
    // VITE_API_URL is the Render backend origin (e.g. https://foo.onrender.com) —
    // the WS endpoint lives there too, not on location.host (that's Vercel).
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

    const connect = () => {
      if (stopped) return
      ws = new WebSocket(wsUrl())

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
