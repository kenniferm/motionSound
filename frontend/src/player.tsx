import {useCallback, useEffect, useRef, useState} from 'react'
import {api, ApiError, stageName, timeText} from './api'
import type {Peaks, Track} from './api'
import {Icon} from './Icon'

const envelopes = new Map<string, Promise<Peaks>>()

function usePeaks(track?: Track | null) {
  const id = track?.id
  const [result, setResult] = useState<{id: string; peaks: number[]}>()
  useEffect(() => {
    if (!id) return
    let active = true
    let request = envelopes.get(id)
    if (!request) {
      request = api<Peaks>('/audio/' + id + '/waveform')
      envelopes.set(id, request)
      if (envelopes.size > 256) envelopes.delete(envelopes.keys().next().value!)
    }
    request.then(
      data => { if (active) setResult({id, peaks: data.peaks}) },
      () => envelopes.delete(id),
    )
    return () => { active = false }
  }, [id])
  return id && result?.id === id ? result.peaks : []
}

function Waveform({track, progress}: {track?: Track | null; progress: number}) {
  const peaks = usePeaks(track)
  const count = 144
  return <span className={peaks.length ? 'waveform' : 'waveform empty-waveform'} aria-hidden="true">
    {peaks.length > 0 && <svg viewBox={`0 0 ${count * 4} 48`} preserveAspectRatio="none">
      {Array.from({length: count}, (_, index) => {
        const start = Math.floor(index * peaks.length / count)
        const end = Math.max(start + 1, Math.floor((index + 1) * peaks.length / count))
        const peak = Math.max(...peaks.slice(start, end))
        const height = Math.max(1.25, peak * 44)
        return <rect key={index} x={index * 4} y={(48 - height) / 2} width="2.55" height={height} rx=".8"
          className={index / count < progress ? 'elapsed' : undefined}/>
      })}
    </svg>}
  </span>
}

export type QueueScope = 'round' | 'saved'

export function usePlayer() {
  const audio = useRef<HTMLAudioElement>(null)
  const current = useRef<Track | null>(null)
  const [queue, setQueue] = useState<Track[]>([])
  const [queueScope, setQueueScope] = useState<QueueScope>('round')
  const [track, setTrack] = useState<Track | null>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [mediaDuration, setMediaDuration] = useState(0)
  const [error, setError] = useState('')
  const [volume, setVolume] = useState(1)

  const select = useCallback((next: Track, lineup?: Track[], autoplay = true, scope: QueueScope = 'round') => {
    const element = audio.current
    if (!element) return
    if (lineup) {
      setQueue(lineup)
      setQueueScope(scope)
    }
    if (next.id !== current.current?.id) {
      current.current = next
      setTrack(next)
      setTime(0)
      setMediaDuration(0)
      setError('')
      element.src = next.url
    }
    if (autoplay) {
      const id = next.id
      setError('')
      element.play().catch(reason => {
        if (reason?.name !== 'AbortError' && current.current?.id === id) setError('再生できませんでした')
      })
    }
  }, [])

  const toggle = useCallback((next?: Track, lineup?: Track[], scope?: QueueScope) => {
    const element = audio.current
    if (!element) return
    if (next && next.id !== current.current?.id) {
      select(next, lineup, true, scope)
    } else if (!element.paused && !element.ended) element.pause()
    else if (current.current) select(current.current, lineup, true, scope)
  }, [select])

  const stop = useCallback((message = '') => {
    const element = audio.current
    if (element) {
      element.pause()
      element.removeAttribute('src')
      element.load()
    }
    current.current = null
    setQueue([])
    setTrack(null)
    setPlaying(false)
    setTime(0)
    setMediaDuration(0)
    setError(message)
  }, [])

  const stopForRoom = useCallback((roomId: string) => {
    if (current.current?.roomId === roomId) stop('再生していた部屋が削除されました')
  }, [stop])

  const replaceQueue = useCallback((lineup: Track[]) => {
    if (lineup.some(item => item.candidateId === current.current?.candidateId)) {
      setQueue(previous => previous.length === lineup.length &&
        previous.every((item, position) => item.id === lineup[position].id && item.title === lineup[position].title)
        ? previous : lineup)
    }
  }, [])

  useEffect(() => {
    const element = audio.current
    if (!element) return
    const controller = new AbortController()
    const on = (name: string, change: () => void) => element.addEventListener(name, change, {signal: controller.signal})
    const updateClock = () => setTime(element.currentTime)
    on('timeupdate', updateClock)
    on('seeked', updateClock)
    on('loadedmetadata', () => setMediaDuration(Number.isFinite(element.duration) ? element.duration : 0))
    const updatePlaying = () => setPlaying(!element.paused && !element.ended)
    for (const name of ['play', 'pause', 'ended', 'emptied']) on(name, updatePlaying)
    on('volumechange', () => setVolume(element.volume))
    on('error', () => { if (current.current) setError('音源を読み込めませんでした') })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!track) return
    const id = track.id
    const controller = new AbortController()
    let timer = 0
    async function verify() {
      try {
        const next = await api<Track>('/audio/' + id + '/status', {signal: controller.signal})
        if (controller.signal.aborted || current.current?.id !== id) return
        if (next.title !== current.current.title) {
          current.current = next
          setTrack(next)
        }
      } catch (reason) {
        if (current.current?.id === id && reason instanceof ApiError && [404, 410].includes(reason.status)) {
          stop('再生していた音源が削除または置き換えられました')
        }
      } finally {
        if (!controller.signal.aborted && current.current?.id === id) timer = window.setTimeout(verify, 1200)
      }
    }
    timer = window.setTimeout(verify, 1200)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [track?.id, stop])

  function seek(seconds: number) {
    if (audio.current && mediaDuration > 0) {
      audio.current.currentTime = Math.max(0, Math.min(mediaDuration, seconds))
      setTime(audio.current.currentTime)
    }
  }

  const index = queue.findIndex(item => item.id === track?.id ||
    track?.stage !== 'final' && item.candidateId === track?.candidateId)
  function step(offset: number) {
    const target = queue[index + offset]
    if (index >= 0 && target) select(target)
  }

  // The selected source and its lineup live independently of the visible room.
  return {audio, track, playing, time, duration: mediaDuration || track?.duration || 0, canSeek: mediaDuration > 0,
    error, volume, select, toggle, stopForRoom, seek, step, replaceQueue, queueScope,
    previous: index > 0, next: index >= 0 && index + 1 < queue.length,
    setVolume: (value: number) => { if (audio.current) audio.current.volume = value },
  }
}

export type Player = ReturnType<typeof usePlayer>

export function PlayerBar({player}: {player: Player}) {
  const {track, duration, time, error, playing} = player
  const progress = duration > 0 ? Math.min(1, time / duration) : 0
  return <footer className="player-bar" aria-label="共通プレーヤー">
    <audio ref={player.audio} preload="metadata"/>
    <div className="transport">
      <button type="button" className="icon-button" aria-label="前の音源" disabled={!player.previous} onClick={() => player.step(-1)}><Icon name="previous"/></button>
      <button type="button" className="play-button main-play" aria-label={playing ? '一時停止' : '再生'} disabled={!track} onClick={() => player.toggle()}><Icon name={playing ? 'pause' : 'play'} size={23}/></button>
      <button type="button" className="icon-button" aria-label="次の音源" disabled={!player.next} onClick={() => player.step(1)}><Icon name="next"/></button>
    </div>
    <div className="now-playing">
      <strong>{track?.title ?? '試聴プレーヤー'}</strong>
      {error ? <span role="status" className="player-error">{error}</span> : track && <span>{stageName[track.stage]}</span>}
    </div>
    <div className="seek-track">
      <Waveform track={track} progress={progress}/>
      <span className="playhead" style={{left: (progress * 100) + '%'}} aria-hidden="true" hidden={!track}/>
      <input aria-label="再生位置" aria-valuetext={timeText(time)} type="range" min={0} max={duration || 1} step={0.01}
        value={Math.min(time, duration)} disabled={!player.canSeek} onChange={event => player.seek(event.currentTarget.valueAsNumber)}/>
    </div>
    <span className="player-time" aria-label="再生時間">{timeText(time)} <span>/</span> {timeText(duration)}</span>
    <label className="player-volume">音量 <input type="range" min="0" max="1" step=".01" value={player.volume} onChange={event => player.setVolume(event.currentTarget.valueAsNumber)}/></label>
  </footer>
}
