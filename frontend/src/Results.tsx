import {useState} from 'react'
import type {FormEvent, ReactNode} from 'react'
import {api, dateText, errorMessage, post, providerName, roundTracks, stageName, timeText} from './api'
import type {Candidate, Round, Track} from './api'
import {Icon} from './Icon'
import type {Player, QueueScope} from './player'
import {ReviewForm} from './ReviewForm'
import {useResource} from './useResource'

export type ResultActions = {
  improve: (track: Track) => void
  edit: (round: Round) => void
  roundAction: (round: Round, action: 'cancel' | 'retry' | 'undo', position?: number) => Promise<void>
  openSpatial: (track: Track, lineup: Track[], scope: QueueScope) => void
  prepareSpatial: (candidateId: string, layerId: string) => Promise<void>
  changed: () => void
}

export function RenameForm({name, onSave, onClose}: {name: string; onSave: (name: string) => Promise<void>; onClose: () => void}) {
  const [draft, setDraft] = useState(name)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving || !draft.trim()) return
    setSaving(true)
    try { await onSave(draft); onClose() }
    catch (reason) { setError(errorMessage(reason)); setSaving(false) }
  }
  return <form className="rename-form" onSubmit={submit}>
    <input aria-label="新しい名前" autoFocus required maxLength={80} value={draft} onChange={event => setDraft(event.target.value)}/>
    <div className="action-row">
      <button type="submit" disabled={saving}>保存</button>
      <button type="button" onClick={onClose}>閉じる</button>
    </div>
    {error && <span className="error" role="alert">{error}</span>}
  </form>
}

function Disclosure({label, children, keepMounted = false}: {label: string; children: ReactNode; keepMounted?: boolean}) {
  const [open, setOpen] = useState(false)
  return <details className="disclosure" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {(keepMounted || open) && children}
  </details>
}

const planLabels: Record<string, string> = {
  sonic_intent: '音の狙い', synthesis_principle: '音の作り方', synthesis_strategy: '音の作り方',
  generation_principle: '音の作り方', time_form: '時間構成', temporal_structure: '時間構成',
  listening_check: '試聴のポイント', audible_identity: '音の特徴',
  summary: '構想', concept: '構想', sound_description: '音の特徴',
  production_brief: '制作方針', production_plan: '制作方針', design: '設計',
  description: '音の特徴', listening_goal: '試聴のポイント', notes: '制作メモ',
  source_descriptions: '各音源の役割', edits: '変更する音', retained: '残す音',
  phenomena: '音の変化', composition: '構成', form: '構成', artistic_intention: '表現の狙い',
}

function Plan({plan}: {plan: Record<string, unknown>}) {
  return <Disclosure label="制作方針"><div className="plan">
    {Object.entries(plan).filter(([key]) => key !== 'title').map(([key, value]) => {
      if (!value || (Array.isArray(value) && !value.length)) return null
      const show = typeof value === 'string' || Array.isArray(value) && value.every(item => typeof item === 'string')
      if (show) return <div key={key}>{planLabels[key] && <h4>{planLabels[key]}</h4>}<p>{Array.isArray(value) ? value.join('\n') : String(value)}</p></div>
      if (key === 'source_descriptions' && typeof value === 'object') return <dl key={key}>
        {Object.entries(value).map(([source, notes]) => <div key={source}><dt>{source}</dt><dd>{String(notes)}</dd></div>)}
      </dl>
      return null
    })}
    <details><summary>計画データ</summary><pre>{JSON.stringify(plan, null, 2)}</pre></details>
  </div></Disclosure>
}

function Codes({track}: {track: Track}) {
  const {snapshot} = useResource<{codes: {label: string; source: string}[]}>('/audio/' + track.id + '/codes')
  const [copied, setCopied] = useState('')
  return <div className="codes">
    {snapshot?.error && <p className="error" role="alert">{snapshot.error}</p>}
    {!snapshot && <p>読み込み中…</p>}
    {snapshot?.data?.codes.map(code => <details key={code.label}>
      <summary>{code.label}</summary>
      <button type="button" onClick={() => void navigator.clipboard.writeText(code.source).then(
        () => setCopied(code.label), () => setCopied('コピーに失敗しました'),
      )}>{copied === code.label ? 'コピーしました' : 'コードをコピー'}</button>
      {copied === 'コピーに失敗しました' && <p role="status">{copied}</p>}
      <pre tabIndex={0}>{code.source}</pre>
    </details>)}
  </div>
}

function PlayButton({track, player, lineup, label, scope}: {
  track: Track; player: Player; lineup?: Track[]; label: string; scope?: QueueScope
}) {
  const playing = player.track?.id === track.id && player.playing
  return <button type="button" className="play-button" onClick={() => player.toggle(track, lineup, scope)}
    aria-label={label + (playing ? 'を一時停止' : 'を再生')}><Icon name={playing ? 'pause' : 'play'}/></button>
}

type CardProps = {
  player: Player
  lineup: Track[]
  queueScope?: QueueScope
  actions: ResultActions
  active?: boolean
  roomActive?: boolean
  retry?: () => Promise<void>
  retrying?: boolean
} & ({candidate: Candidate; track?: never} | {track: Track; candidate?: never})

export function CandidateCard({candidate, track: savedTrack, player, lineup, queueScope = 'round', actions, active, roomActive, retry, retrying}: CardProps) {
  const track = savedTrack ?? candidate?.track
  const {id, previews = [], title = track?.title, unavailableSpatial, error, plan, canRetry} = candidate ?? {}
  const state = candidate?.status ?? 'ready'
  const [operation, setOperation] = useState<'rename' | 'review' | null>(null)
  const [reviewOpened, setReviewOpened] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [previewPending, setPreviewPending] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const selected = track?.id === player.track?.id && Boolean(track)
  const status = active ? state === 'running' ? '生成中' : '準備中' : '未完成'

  async function save() {
    if (!track) return
    try { await post('/audio/' + track.id + '/save'); setSaved(true); actions.changed() }
    catch (reason) { setSaveError(errorMessage(reason)) }
  }

  async function prepareSpatial() {
    if (!id || !unavailableSpatial || previewPending) return
    setPreviewPending(true)
    setPreviewError('')
    try { await actions.prepareSpatial(id, unavailableSpatial.layerId) }
    catch (reason) { setPreviewError(errorMessage(reason)) }
    finally { setPreviewPending(false) }
  }

  return <article className={'candidate-card' + (selected ? ' selected' : '')} aria-label={title} data-state={state}>
    <div className="candidate-top">
      {track ? <PlayButton track={track} lineup={lineup} player={player} label={track.title} scope={queueScope}/> : <span className="candidate-marker"><Icon name="mark"/></span>}
      <div className="candidate-title">
        <h3>{title}</h3>
        <span>{providerName[candidate?.provider ?? track?.provider ?? 'unknown']}{track && ' · ' + timeText(track.duration)}</span>
      </div>
      {track && <a className="icon-button download" href={track.url + '?download=1'} aria-label={title + 'をダウンロード'}><Icon name="download"/></a>}
    </div>
    {error && <p className="error" role="status">{error}</p>}
    {!track && !error && <p className="muted candidate-state">{status}</p>}
    {!track && retry && canRetry && <button className="retry-button" type="button" disabled={retrying || (roomActive && !active)} onClick={() => void retry()}>
      {retrying ? '再開を受付中…' : 'この候補を再開'}
    </button>}
    {track && <div className="action-row candidate-actions">
      <button type="button" className="improve-button" onClick={() => actions.improve(track)}><Icon name="sparkle" size={16}/>この作品を改善</button>
      {track.hasSpatialView && <button type="button" className="icon-button" aria-label={title + 'の3D音源ビュー'} onClick={() => actions.openSpatial(track, lineup, queueScope)}><Icon name="grid"/></button>}
    </div>}
    {plan && <Plan plan={plan}/>}
    {(track || previews.length > 0 || unavailableSpatial) && <div className="preview-list">
      {[...previews, ...(track ? [track] : [])].map(preview => <div className="preview" key={preview.id}>
        <PlayButton track={preview} player={player} lineup={lineup} label={stageName[preview.stage]} scope={queueScope}/>
        <span>{stageName[preview.stage]}</span>
        {preview.stage !== 'final' && preview.hasSpatialView && <button type="button" className="icon-button" aria-label={stageName[preview.stage] + 'の3D音源ビュー'}
          onClick={() => actions.openSpatial(preview, lineup, queueScope)}><Icon name="grid"/></button>}
      </div>)}
      {unavailableSpatial && <div>
        {previewError || unavailableSpatial.error
          ? <p className="error" role="alert">{previewError || unavailableSpatial.error}</p>
          : <p className="muted">空間設計の試聴音が未保存です。</p>}
        <button type="button" disabled={previewPending || roomActive} onClick={() => void prepareSpatial()}>
          {(previewError || unavailableSpatial.error) ? '空間設計の試聴音を再試行' : '空間設計の試聴音を復元'}
        </button>
      </div>}
    </div>}
    {track && <Disclosure label="作品の詳細" keepMounted>
      <div className="action-row card-detail-actions">
        <button type="button" aria-expanded={operation === 'review'} onClick={() => {
          setReviewOpened(true)
          setOperation(operation === 'review' ? null : 'review')
        }}>レビュー</button>
        <button type="button" onClick={() => setOperation(operation === 'rename' ? null : 'rename')}>名前を変更</button>
        <button type="button" onClick={() => void save()} disabled={saved}>{saved ? '保存済み' : '作品を保存'}</button>
      </div>
      {saveError && <p className="error" role="alert">{saveError}</p>}
      {operation === 'rename' && <RenameForm name={track.title} onClose={() => setOperation(null)} onSave={async name => {
        await api('/audio/' + track.id, {method: 'PATCH', body: {name}})
        actions.changed()
      }}/>}
      {reviewOpened && <div hidden={operation !== 'review'}><ReviewForm key={track.id} track={track}/></div>}
      <Disclosure label="Pythonコード"><Codes track={track}/></Disclosure>
    </Disclosure>}
  </article>
}

export function RoundSection({round, player, actions, roomActive}: {
  round: Round
  player: Player
  actions: ResultActions
  roomActive: boolean
}) {
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const lineup = roundTracks(round)
  const unfinished = lineup.length < Math.max(round.count, round.candidates.length)
  const recoverable = round.status === 'awaiting_approval' || round.status === 'failed' ||
      round.status === 'cancelled' || round.status === 'interrupted' || round.status === 'partial' || unfinished
  async function act(action: 'cancel' | 'retry' | 'undo', position?: number) {
    if (pending) return
    setPending(action)
    setError('')
    try { await actions.roundAction(round, action, position) }
    catch (reason) { setError(errorMessage(reason)) }
    finally { setPending('') }
  }

  return <details className="round" open>
    <summary>
      <div className="round-heading">
        <span className={'state-dot' + (round.task ? ' in-progress' : '')}/>
        <div>
          <span className="round-title">{round.prompt}</span>
          <div className="round-meta">
            {round.parent ? '改善' : '生成'}{round.count > 0 && ' · ' + round.count + '案'}
            {round.duration && ' · ' + timeText(round.duration)}
            {round.created && ' · ' + dateText(round.created)}
          </div>
        </div>
      </div>
      <Icon name="chevron"/>
    </summary>
    <div className="round-content">
      <div className="action-row round-actions">
        {!round.task && <button className="text-button" type="button" disabled={roomActive || pending !== ''} onClick={() => actions.edit(round)}><Icon name="edit" size={15}/>指示を編集</button>}
        {round.previous && <button className="text-button" type="button" disabled={roomActive || pending !== ''} onClick={() => void act('undo')}><Icon name="undo" size={15}/>ひとつ前に戻す</button>}
        {round.task
          ? <button className="text-button" type="button" disabled={round.task.stopping || pending !== ''} onClick={() => void act('cancel')}><Icon name="stop" size={15}/>{round.task.stopping ? '中止しています…' : '生成を中止'}</button>
          : recoverable && <button type="button" className="text-button" disabled={roomActive || pending !== ''} onClick={() => void act('retry')}>
            {round.status === 'awaiting_approval' ? '保存したコードを実行' : '生成を再開'}
          </button>}
      </div>
      {round.task && <div className="progress-block">
        <div><span>{round.task.label}</span><span>約{round.task.progress}%</span></div>
        <progress value={round.task.progress} max="100" aria-label="生成の進捗"/>
      </div>}
      {round.status === 'cancelled' && !round.task && <p className="muted" role="status">生成を中止しました</p>}
      {(error || round.error) && <p className="error" role="alert">{error || round.error}</p>}
      {round.candidates.length > 0 && <div className="candidate-grid">
        {round.candidates.map(candidate => <CandidateCard key={candidate.position} candidate={candidate} player={player}
          lineup={lineup} actions={actions} active={Boolean(round.task)} roomActive={roomActive}
          retrying={pending === 'retry'}
          retry={() => act('retry', candidate.position)}/>)}
      </div>}
    </div>
  </details>
}
