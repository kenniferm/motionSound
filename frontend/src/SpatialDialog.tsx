import {useEffect, useRef} from 'react'
import type {RefObject} from 'react'
import {stageName} from './api'
import type {Track} from './api'
import {Icon} from './Icon'
import {useResource} from './useResource'
import {bindSpatialViewer} from './spatial-view.js'

type SpatialData = {
  rate: number
  referenceDistance: number
  maxDirectGain: number
  sources: unknown[]
}

export function SpatialDialog({track, audio, close}: {track: Track; audio: RefObject<HTMLAudioElement | null>; close: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const {snapshot} = useResource<SpatialData>('/audio/' + track.id + '/spatial')
  const data = snapshot?.data

  useEffect(() => {
    dialog.current?.showModal()
  }, [])

  useEffect(() => {
    if (!dialog.current || !audio.current || !data) return
    return bindSpatialViewer(dialog.current, audio.current, data)
  }, [audio, data])

  return <dialog ref={dialog} className="spatial-dialog" aria-label="3D音源ビュー" onClose={close}>
    <div className="section-head"><strong>{track.title} · {stageName[track.stage]}</strong>
      <button type="button" className="icon-button close" aria-label="閉じる" onClick={() => dialog.current?.close()}><Icon name="close"/></button>
    </div>
    {snapshot?.error && <p className="error" role="alert">{snapshot.error}</p>}
    {!snapshot && <p role="status">空間情報を読み込んでいます…</p>}
    {data && <>
      <canvas role="img" aria-label="3D空間での音の広がり"/>
      <div className="controls">
        <button type="button" className="play">再生</button>
        <input className="seek" type="range" min="0" step="0.01" defaultValue="0" aria-label="空間ビューの再生位置"/>
        <output className="time"/>
        <label>音量 <input className="volume" type="range" min="0" max="1" step="0.01" defaultValue="1"/></label>
      </div>
      <div className="controls">
        <label>表示
          <select className="view" aria-label="表示">
            <option value="field">音の広がり</option>
            <option value="wave">音源の波形</option>
          </select>
        </label>
        <label>音源 <select className="source"><option value="">すべて</option></select></label>
        <button type="button" className="closer" aria-label="拡大">＋</button>
        <button type="button" className="farther" aria-label="縮小">−</button>
        <button type="button" className="reset">視点を戻す</button>
        <span className="bass-legend" hidden>金色 · 重低音 20–80 Hz</span>
      </div>
      <div className="controls field-controls">
        <label>境界レベル <input className="field-level" type="range" min="-24" max="-6" step="1" defaultValue="-12"/>
          <output className="field-value">−12 dB</output>
        </label>
        <span className="field-note">直達音の推定 · 素材の最大RMSを基準</span>
      </div>
    </>}
  </dialog>
}
