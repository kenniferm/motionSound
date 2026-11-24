import {useId, useRef, useState} from 'react'
import type {FormEvent} from 'react'
import {dateText, errorMessage, post} from './api'
import type {Review, ReviewField, Track} from './api'
import {useResource} from './useResource'

export function ReviewForm({track}: {track: Track}) {
  const path = '/audio/' + track.id + '/reviews'
  const {snapshot, reload} = useResource<{reviews: Review[]}>(path)
  const {snapshot: fields} = useResource<{fields: ReviewField[]}>('/review-fields')
  const fieldPrefix = useId()
  const [text, setText] = useState('')
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const submission = useRef<{key: string; token: string} | undefined>(undefined)
  const nonempty = Object.fromEntries(Object.entries(answers).filter(([, value]) => value))
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || (!text.trim() && !Object.keys(nonempty).length)) return
    setPending(true)
    const body = {text, answers: Object.fromEntries(Object.entries(nonempty).sort())}
    const key = JSON.stringify([track.id, body])
    if (submission.current?.key !== key) submission.current = {key, token: crypto.randomUUID()}
    try {
      await post(path, {...body, token: submission.current.token})
      setText('')
      setAnswers({})
      submission.current = undefined
      setMessage('レビューを保存しました')
      reload()
    } catch (reason) {
      setMessage(errorMessage(reason))
    } finally { setPending(false) }
  }
  return <div className="review">
    <form onSubmit={save}>
      <label>聴いた感想<textarea rows={3} disabled={pending} value={text} onChange={event => setText(event.target.value)}/></label>
      {fields?.data && <details className="review-fields">
        <summary>詳しく評価</summary>
        {fields.data.fields.map(field => <div className="review-choice" key={field.id}>
          <label htmlFor={fieldPrefix + field.id}>{field.label}</label>
          <select id={fieldPrefix + field.id} disabled={pending} value={answers[field.id] || ''}
            onChange={event => setAnswers({...answers, [field.id]: event.target.value})}>
            <option value="">選択なし</option>
            {Object.entries(field.options).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </div>)}
      </details>}
      <button disabled={pending || (!text.trim() && !Object.keys(nonempty).length)} type="submit">レビューを保存</button>
      {message && <p role="status">{message}</p>}
    </form>
    {snapshot?.error && <p className="error" role="alert">{snapshot.error}</p>}
    {snapshot?.data?.reviews.map(review => <div className="past-review" key={review.id}>
      <time dateTime={review.created}>{dateText(review.created)}</time>
      {review.run_id === null && <span className="muted">試聴音源が未記録の旧レビュー</span>}
      <p>{review.text}</p>
      {Object.entries(review.context.questionnaire?.answers || {}).map(([key, choice]) => {
        const field = fields?.data?.fields.find(item => item.id === key)
        return <span key={key} className="review-answer">{field?.label ?? key} · {field?.options[choice] ?? choice}</span>
      })}
    </div>)}
  </div>
}
