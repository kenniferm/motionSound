export type Stage = 'original' | 'spatial' | 'final'
export type Provider = 'openai' | 'claude' | 'demo' | 'unknown'

export const providerName: Record<Provider, string> = {
  openai: 'OpenAI',
  claude: 'Claude',
  demo: 'デモ',
  unknown: '生成元不明',
}

export const stageName: Record<Stage, string> = {
  original: '原音',
  spatial: '空間設計',
  final: 'アレンジ後の完成版',
}

export type Track = {
  id: string
  roomId: string
  roundId: string
  candidateId: string
  title: string
  duration: number
  url: string
  stage: Stage
  provider?: Provider
  format: 'mp3' | 'wav'
  hasSpatialView: boolean
  canPartialImprove: boolean
}
export type RoomSummary = {id: string; title: string; created: string; active: boolean}
export type History = {
  rooms: RoomSummary[]
  failedDeletions: {roomId: string; title: string; error: string}[]
}
export type Candidate = {
  id?: string
  position: number
  title: string
  provider: Provider
  status: 'ready' | 'failed' | 'running' | 'waiting'
  plan: Record<string, unknown> | null
  track: Track | null
  previews: Track[]
  unavailableSpatial?: {layerId: string; error: string | null} | null
  canRetry?: boolean
  error: string | null
  checkpoint: string | null
}
export type Round = {
  id: string
  prompt: string
  status: string
  duration?: number
  count: number
  created?: string
  previous?: string
  parent?: string
  error?: string
  task?: {id: string; progress: number; label: string; stopping: boolean}
  candidates: Candidate[]
}
export type Room = {id: string; rounds: Round[]; active: boolean; errors: string[]}
export type Review = {id: string; text: string; created: string; run_id: string | null; context: {questionnaire?: {answers: Record<string, string>}}}
export type ReviewField = {id: string; label: string; options: Record<string, string>}
export type Submitted = {roomId: string; roundId: string}
export type Peaks = {peaks: number[]; duration: number}

export function roundTracks(round: Round) {
  return round.candidates.flatMap(({track}) => track ? [track] : [])
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

export async function api<T>(path: string, options: {method?: string; body?: unknown; signal?: AbortSignal} = {}): Promise<T> {
  const {method, body, signal} = options
  const response = await fetch('/api' + path, {
    method, signal,
    ...(body !== undefined && {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}),
  })
  if (!response.ok) {
    const problem = await response.json().catch(() => null)
    throw new ApiError(problem?.error || '接続先の応答を確認できませんでした', response.status)
  }
  return response.json()
}

export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return api<T>(path, {method: 'POST', body})
}

export function roundPath(room: string, round: string) {
  return '/rooms/' + room + '/rounds/' + round
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '処理中にエラーが発生しました'
}

const dateFormatter = new Intl.DateTimeFormat('ja-JP', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'})

export function dateText(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : dateFormatter.format(date)
}

export function timeText(seconds: number) {
  const whole = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0
  return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0')
}
