import { useEffect, useRef } from 'react'

export function useWebSocket(onMessage: (msg: unknown) => void) {
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  useEffect(() => {
    let ws: WebSocket
    let retryDelay = 1000
    let stopped = false

    const connect = () => {
      if (stopped) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws/updates`)

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
