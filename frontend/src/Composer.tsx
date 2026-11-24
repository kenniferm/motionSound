import {useRef, useState} from 'react'
import type {FormEvent} from 'react'
import {errorMessage, post, roundPath} from './api'
import type {Round, Submitted, Track} from './api'
import {Icon} from './Icon'

export type ComposerMode =
  | {kind: 'new'}
  | {kind: 'improve'; source: Track}
  | {kind: 'redo'; room: string; round: Round}

const DEFAULT_DURATION = 60
const DEFAULT_COUNT = 1

export function Composer({mode, onClear, onSubmit}: {
  mode: ComposerMode
  onClear: () => void
  onSubmit: (response: Submitted, editedSinceSubmit: boolean) => void
}) {
  const {kind} = mode
  const [prompt, setPrompt] = useState(mode.kind === 'redo' ? mode.round.prompt : '')
  const originalDuration = mode.kind === 'improve' ? mode.source.duration : null
  const supportsPartial = mode.kind === 'improve' && mode.source.canPartialImprove
  const [duration, setDuration] = useState(String(originalDuration && originalDuration >= 5 && originalDuration <= 240
    ? Math.round(originalDuration) : DEFAULT_DURATION))
  const [count, setCount] = useState(String(DEFAULT_COUNT))
  const [compare, setCompare] = useState(false)
  const [remake, setRemake] = useState(mode.kind === 'improve' && !supportsPartial)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [suggestions, setSuggestions] = useState<string[]>([])
  const [polishedPrompt, setPolishedPrompt] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const form = useRef<HTMLFormElement>(null)
  const edits = useRef(0)
  const partial = kind === 'improve' && !remake
  const title = kind === 'improve' ? '作品を改善' : kind === 'redo' ? '指示を編集' : '音の制作'
  const createLabel = kind === 'improve' ? '改善した作品を生成' : kind === 'redo' ? 'この指示でやり直す' : '音を生成'

  function replacePrompt(next: string) {
    edits.current += 1
    setPrompt(next)
    setPolishedPrompt('')
    input.current?.focus()
  }

  async function act(action: string, run: () => Promise<void>) {
    if (pending) return
    setPending(action)
    setError('')
    try { await run() }
    catch (reason) { setError(errorMessage(reason)) }
    finally { setPending('') }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!prompt.trim()) return
    void act('submit', async () => {
      const submittedEdits = edits.current
      const settings = {prompt, duration: partial ? originalDuration : Number(duration), count: Number(count)}
      const request = mode.kind === 'redo'
        ? {path: roundPath(mode.room, mode.round.id) + '/redo', body: {prompt}}
        : mode.kind === 'improve'
          ? {path: '/audio/' + mode.source.id + '/improve', body: {...settings, remake}}
          : {path: '/rooms', body: {...settings, compare}}
      const response = await post<Submitted>(request.path, request.body)
      onSubmit(response, edits.current !== submittedEdits)
    })
  }

  function polish() {
    void act('polish', async () => {
      setPolishedPrompt('')
      const result = await post<{prompt: string}>('/prompt/polish', {prompt, refinement: kind === 'improve'})
      if (!input.current) return
      if (input.current.value === prompt) replacePrompt(result.prompt)
      else setPolishedPrompt(result.prompt)
    })
  }

  function suggest(track: string) {
    void act('suggest', async () => {
      const result = await post<{prompts: string[]}>('/audio/' + track + '/suggestions')
      setSuggestions(result.prompts)
    })
  }

  return <section className="composer" aria-labelledby="composer-heading">
    <div className="section-head">
      <h1 id="composer-heading">{title}</h1>
      {kind !== 'new' && <button type="button" className="icon-button" aria-label="入力を閉じる" onClick={onClear}><Icon name="close"/></button>}
    </div>
    {mode.kind === 'improve' && <div className="source-name"><Icon name="mark"/><span>{mode.source.title}</span></div>}
    <form ref={form} onSubmit={submit} onChange={() => { edits.current += 1 }}>
      <div className="prompt-field">
        <label htmlFor="prompt">{kind === 'improve' ? 'どう改善したい？' : '音の指示'}</label>
        <textarea ref={input} id="prompt" value={prompt} required rows={8}
          autoFocus={kind !== 'new'}
          placeholder={kind === 'improve' ? '変えたい音や、そのまま残したい音を入力' : '作りたい音の情景や聴こえ方を入力'}
          onChange={event => setPrompt(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
              event.preventDefault()
              form.current?.requestSubmit()
            }
          }}/>
        <div className="prompt-tools">
          <button type="button" className="icon-button" aria-label="指示を整える" disabled={!prompt.trim() || Boolean(pending)} onClick={polish}><Icon name="sparkle"/>{pending === 'polish' && '整えています'}</button>
          {mode.kind === 'improve' && <button type="button" className="text-button" disabled={Boolean(pending)} onClick={() => suggest(mode.source.id)}>
            {pending === 'suggest' ? '改善案を考えています…' : 'AIに改善案を聞く'}
          </button>}
        </div>
      </div>
      {polishedPrompt && <div className="suggestions">
        <p className="muted">{polishedPrompt}</p>
        <button type="button" onClick={() => replacePrompt(polishedPrompt)}>整えた指示を使う</button>
      </div>}
      {suggestions.length > 0 && <div className="suggestions" aria-label="改善案">
        {suggestions.map((suggestion, index) => <button type="button" key={index}
          onClick={() => replacePrompt(suggestion)}>{suggestion}</button>)}
      </div>}
      {kind !== 'redo' && <div className="composer-settings">
        <label>長さ（秒）<input required type="number" min="5" max="240" readOnly={partial}
          value={partial ? originalDuration ?? duration : duration} onChange={event => setDuration(event.target.value)}/></label>
        <label>生成する数<input required type="number" min="1" max="5" value={count} onChange={event => setCount(event.target.value)}/></label>
      </div>}
      {kind === 'new' && <label className="checkbox-row"><input type="checkbox" checked={compare} onChange={event => setCompare(event.target.checked)}/> Claudeでも生成して聴き比べる</label>}
      {kind === 'improve' && <>
        <label className="checkbox-row"><input type="checkbox" checked={remake} disabled={!supportsPartial} onChange={event => setRemake(event.target.checked)}/> 作品全体を作り直す</label>
        {partial && <p className="muted">長さを変える場合は、作品全体を作り直してください。</p>}
        {!supportsPartial && <p className="muted">個別素材がない作品は、全体を作り直せます。</p>}
      </>}
      {error && <p role="alert" className="error">{error}</p>}
      <button className="generate-button" type="submit" disabled={!prompt.trim() || Boolean(pending)}>
        <Icon name="mark"/>{pending === 'submit' ? '生成を受け付けています…' : createLabel}<Icon name="arrow"/>
      </button>
    </form>
  </section>
}
