import type {ReactNode} from 'react'

const paths = {
  mark: <path d="M3 12h3m3-5v10m3-13v16m3-13v10m3-5h3"/>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
  plus: <path d="M12 5v14M5 12h14"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  chevron: <path d="m8 10 4 4 4-4"/>,
  edit: <><path d="m4 20 4-.8L20 7.1 16.9 4 5 16z"/><path d="m14.8 6.1 3.1 3.1"/></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v6m4-6v6"/></>,
  play: <path d="M8 5.5v13l11-6.5z" fill="currentColor" stroke="none"/>,
  pause: <path d="M9 5v14M15 5v14" strokeWidth="3"/>,
  previous: <path d="M6 5v14m12-13L8 12l10 6z" fill="currentColor"/>,
  next: <path d="M18 5v14M6 6l10 6-10 6z" fill="currentColor"/>,
  download: <path d="M12 3v12m-5-5 5 5 5-5M5 16v4h14v-4"/>,
  sparkle: <path d="M12 3c.7 5 3.1 7.3 8 8-4.9.7-7.3 3-8 8-.7-5-3.1-7.3-8-8 4.9-.7 7.3-3 8-8Zm7 12v6m-3-3h6"/>,
  bookmark: <path d="M6 4h12v16l-6-4-6 4z"/>,
  arrow: <path d="M4 12h15m-6-6 6 6-6 6"/>,
  undo: <path d="m8 7-4 4 4 4m-4-4h10a5 5 0 0 1 0 10"/>,
  grid: <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm0 9v9m-8-13.5 8 4.5 8-4.5"/>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
} satisfies Record<string, ReactNode>

export function Icon({name, size = 18}: {name: keyof typeof paths; size?: number}) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}
