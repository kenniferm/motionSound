import {useCallback, useEffect, useRef, useState} from 'react'
import {api, ApiError, errorMessage} from './api'

export function useResource<T>(path: string | null, poll = 0) {
  const [snapshot, setSnapshot] = useState<{path: string; data?: T; error?: string; missing?: boolean}>()
  const refresh = useRef<() => void>(() => {})
  useEffect(() => {
    if (path === null) return
    let timer = 0
    let active = true
    let controller: AbortController | undefined
    async function load() {
      if (!active || path === null) return
      window.clearTimeout(timer)
      controller?.abort()
      const request = controller = new AbortController()
      try {
        const data = await api<T>(path, {signal: request.signal})
        if (active && !request.signal.aborted) setSnapshot({path, data})
      } catch (error) {
        if (active && !request.signal.aborted) {
          const missing = error instanceof ApiError && error.status === 404
          setSnapshot(previous => ({
            path, error: errorMessage(error), missing,
            data: !missing && previous?.path === path ? previous.data : undefined,
          }))
        }
      } finally {
        if (poll && active && !request.signal.aborted) timer = window.setTimeout(load, poll)
      }
    }
    refresh.current = load
    if (poll) window.addEventListener('focus', load)
    void load()
    return () => {
      active = false
      window.clearTimeout(timer)
      controller?.abort()
      window.removeEventListener('focus', load)
      refresh.current = () => {}
    }
  }, [path, poll])
  return {snapshot: snapshot?.path === path ? snapshot : undefined, reload: useCallback(() => refresh.current(), [])}
}
