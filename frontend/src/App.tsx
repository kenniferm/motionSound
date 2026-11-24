import {useEffect, useRef, useState} from 'react'
import {api, dateText, errorMessage, post, roundPath, roundTracks} from './api'
import type {History, Room, Submitted, Track} from './api'
import {Composer} from './Composer'
import type {ComposerMode} from './Composer'
import {Icon} from './Icon'
import {PlayerBar, usePlayer} from './player'
import {CandidateCard, RenameForm, RoundSection} from './Results'
import type {ResultActions} from './Results'
import {SpatialDialog} from './SpatialDialog'
import {useResource} from './useResource'

function currentView(): string | null {
  const query = new URLSearchParams(window.location.search)
  return query.get('view') === 'saved' ? 'saved' : query.get('room')
}

const MOBILE_LAYOUT = '(max-width: 700px)'
const IDLE_ROOM_POLL = 12000
const NEW: ComposerMode = {kind: 'new'}

export function App() {
  const [view, setView] = useState(currentView)
  const [mobile, setMobile] = useState(() => window.matchMedia(MOBILE_LAYOUT).matches)
  const [historyOpen, setHistoryOpen] = useState(false)
  const historyDialog = useRef<HTMLDialogElement>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<ComposerMode>(NEW)
  const [draft, setDraft] = useState(0)
  const currentDraft = useRef(0)
  const [spatial, setSpatial] = useState<Track | null>(null)
  const player = usePlayer()
  const {replaceQueue, track: playerTrack, queueScope} = player
  const selected = view === 'saved' ? null : view
  const history = useResource<History>('/rooms', 1250)
  const rooms = history.snapshot?.data?.rooms ?? []
  const details = useResource<Room>(selected ? '/rooms/' + selected : null,
    rooms.some(item => item.id === selected && item.active) ? 950 : IDLE_ROOM_POLL)
  const saved = useResource<{tracks: Track[]}>(view === 'saved' || (playerTrack && queueScope === 'saved') ? '/saved' : null, IDLE_ROOM_POLL)
  const playingRoomId = queueScope === 'round' ? playerTrack?.roomId : undefined
  const playingDetails = useResource<Room>(playingRoomId && playingRoomId !== selected ? '/rooms/' + playingRoomId : null,
    rooms.some(item => item.id === playingRoomId && item.active) ? 1250 : IDLE_ROOM_POLL)
  const room = details.snapshot?.data
  const failedDeletions = history.snapshot?.data?.failedDeletions
  const tracks = saved.snapshot?.data?.tracks ?? []
  const title = rooms.find(item => item.id === selected)?.title

  useEffect(() => {
    const media = window.matchMedia(MOBILE_LAYOUT)
    const resize = () => {
      historyDialog.current?.close()
      setHistoryOpen(false)
      setMobile(media.matches)
    }
    media.addEventListener('change', resize)
    return () => media.removeEventListener('change', resize)
  }, [])

  useEffect(() => {
    const dialog = historyDialog.current
    if (historyOpen && dialog && !dialog.open) dialog.showModal()
    if (!historyOpen && dialog?.open) dialog.close()
  }, [historyOpen, mobile])

  function updateView(next: string | null, push = true) {
    setView(next)
    const url = new URL(window.location.href)
    url.searchParams.delete('room')
    url.searchParams.delete('view')
    if (next === 'saved') url.searchParams.set('view', 'saved')
    else if (next) url.searchParams.set('room', next)
    window.history[push ? 'pushState' : 'replaceState'](null, '', url)
    setHistoryOpen(false)
    setError('')
  }

  function resetComposer(next: ComposerMode = NEW) {
    currentDraft.current += 1
    setDraft(currentDraft.current)
    setMode(next)
  }

  useEffect(() => {
    const restore = () => {
      setView(currentView())
      resetComposer()
      setHistoryOpen(false)
      setError('')
    }
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [])

  const playingRoom = playingRoomId === selected ? room : playingDetails.snapshot?.data
  const playingRound = playingRoom?.rounds.find(round => round.id === playerTrack?.roundId)
  const savedLineup = saved.snapshot?.data?.tracks
  useEffect(() => {
    if (queueScope === 'saved' && savedLineup) {
      replaceQueue(savedLineup)
    } else if (playingRound) {
      replaceQueue(roundTracks(playingRound))
    }
  }, [playingRound, savedLineup, queueScope, playerTrack?.id, replaceQueue])

  function reload() {
    history.reload()
    details.reload()
    saved.reload()
    playingDetails.reload()
  }

  function open(next: string | null) {
    resetComposer()
    updateView(next)
  }

  function receive(response: Submitted, editedSinceSubmit: boolean) {
    if (draft === currentDraft.current && !editedSinceSubmit) {
      updateView(response.roomId, response.roomId !== currentView())
      resetComposer()
    }
    reload()
  }

  async function remove(roomId: string) {
    try {
      await api('/rooms/' + roomId, {method: 'DELETE'})
      player.stopForRoom(roomId)
      if (currentView() === roomId) {
        resetComposer()
        updateView(null, false)
      }
      reload()
    } catch (reason) { setError(errorMessage(reason)) }
  }

  function focusComposer(next: ComposerMode) {
    resetComposer(next)
    setTimeout(() => document.getElementById('composer-heading')?.scrollIntoView({block: 'nearest'}), 0)
  }

  const actions: ResultActions = {
    improve: source => focusComposer({kind: 'improve', source}),
    edit: round => { if (selected) focusComposer({kind: 'redo', room: selected, round}) },
    roundAction: async (round, action, position) => {
      if (selected) {
        await post(roundPath(selected, round.id) + '/' + action, position === undefined ? undefined : {position})
        reload()
      }
    },
    openSpatial: (track, lineup, scope) => { player.select(track, lineup, false, scope); setSpatial(track) },
    prepareSpatial: async (candidateId, layerId) => {
      await post('/candidates/' + candidateId + '/spatial-preview', {layerId})
      reload()
    },
    changed: reload,
  }

  const historyPanel = <aside className="history" aria-label="生成履歴">
    {mobile && <button type="button" className="icon-button history-close" aria-label="履歴を閉じる"
      onClick={() => historyDialog.current?.close()}><Icon name="close"/></button>}
    <button type="button" className="new-room" onClick={() => open(null)}><Icon name="plus"/>新しく生成</button>
    <button type="button" className={'saved-link' + (view === 'saved' ? ' chosen' : '')} onClick={() => open('saved')}><Icon name="bookmark"/>保存した作品</button>
    <h2>生成履歴</h2>
    {history.snapshot?.error && <p className="error" role="alert">{history.snapshot.error}</p>}
    {failedDeletions?.map(failure => <div className="error" key={failure.roomId}>
      <p role="alert">{failure.title}の削除に失敗しました。{failure.error}</p>
      <button type="button" onClick={() => void remove(failure.roomId)}>削除を再試行</button>
    </div>)}
    {error && <p className="error" role="alert">{error}</p>}
    <nav aria-label="部屋">
      {rooms.map(item => <div className={'history-item' + (view === item.id ? ' chosen' : '')} key={item.id}>
        <div className="history-row">
          <button type="button" className="history-name" aria-current={view === item.id ? 'page' : undefined}
            aria-description={item.active ? '生成中' : undefined} onClick={() => open(item.id)}>
            {item.active && <span className="state-dot in-progress" aria-hidden="true"/>}<span>{item.title}</span>
          </button>
          <button type="button" className="icon-button rename-icon" aria-label={item.title + 'の名前を変更'} onClick={() => setRenaming(renaming === item.id ? null : item.id)}><Icon name="edit" size={15}/></button>
          <button type="button" className="icon-button delete-icon" aria-label={item.title + 'を削除'} onClick={() => void remove(item.id)}><Icon name="trash" size={15}/></button>
        </div>
        <time dateTime={item.created}>{dateText(item.created)}</time>
        {renaming === item.id && <RenameForm name={item.title} onClose={() => setRenaming(null)} onSave={async name => {
          await api('/rooms/' + item.id, {method: 'PATCH', body: {name}})
          reload()
        }}/>}
      </div>)}
    </nav>
  </aside>

  return <div className="studio-shell">
    <header className="topbar">
      {mobile && <button type="button" className="icon-button history-toggle" aria-label="生成履歴" aria-expanded={historyOpen}
        aria-controls="history-dialog" onClick={() => setHistoryOpen(true)}><Icon name="menu"/></button>}
      <div className="brand"><span className="brand-mark"><Icon name="mark" size={21}/></span><span>motionSound</span></div>
      {selected && <span className="top-room">{title}</span>}
      {view === 'saved' && <span className="top-room">保存した作品</span>}
    </header>
    <div className="workspace">
      {mobile
        ? <dialog id="history-dialog" ref={historyDialog} className="history-dialog" aria-label="生成履歴"
          onClose={() => setHistoryOpen(false)}
          onClick={event => { if (event.target === event.currentTarget) event.currentTarget.close() }}>
            {historyPanel}
          </dialog>
        : historyPanel}
      <main className="production">
        <Composer key={draft} mode={mode} onClear={() => resetComposer()} onSubmit={receive}/>
        <section className="results" aria-labelledby="results-heading">
          <div className="section-head">
            <h2 id="results-heading">{view === 'saved' ? '保存した作品' : '生成結果'}</h2>
            {room && room.rounds.length > 0 && <span className="muted result-count">{room.rounds.length}回</span>}
          </div>
          {selected && details.snapshot?.error && <p className="error" role="alert">{details.snapshot.error}</p>}
          {selected && !details.snapshot && <p className="muted" role="status">部屋を開いています…</p>}
          {room?.errors.map((message, index) => <p className="error" role="alert" key={index}>{message}</p>)}
          {room?.rounds.map(round => <RoundSection key={round.id} round={round} player={player} actions={actions} roomActive={room.active}/>)}
          {!view && <div className="empty-results"><p>音を生成すると、ここに作品が並びます。</p></div>}
          {view === 'saved' && <>
            {saved.snapshot?.error && <p className="error" role="alert">{saved.snapshot.error}</p>}
            {saved.snapshot?.data && tracks.length === 0 && <p className="empty-results">保存した作品はありません。</p>}
            <div className="candidate-grid">
              {tracks.map(track => <CandidateCard key={track.id} track={track} player={player}
                actions={actions} lineup={tracks} queueScope="saved"/>)}
            </div>
          </>}
        </section>
      </main>
    </div>
    <PlayerBar player={player}/>
    {spatial && spatial.id === player.track?.id && <SpatialDialog key={spatial.id} track={spatial} audio={player.audio} close={() => setSpatial(null)}/>}
  </div>
}
