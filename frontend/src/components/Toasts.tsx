import { useEffect } from 'react'

// 即時模式的通知：播放時偵測到新狀況（等級變化、新診斷、新的待核准決策、系統自動下發）就跳出提示。

export type Toast = { id: number; kind: 'up' | 'down' | 'info' | 'action'; title: string; body: string }

export function Toasts({ items, onClose }: { items: Toast[]; onClose: (id: number) => void }) {
  useEffect(() => {
    if (!items.length) return
    const timers = items.map((t) => window.setTimeout(() => onClose(t.id), 7000))
    return () => timers.forEach((x) => window.clearTimeout(x))
  }, [items, onClose])
  if (!items.length) return null
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.slice(-4).map((t) => (
        <button key={t.id} className={'toast ' + t.kind} onClick={() => onClose(t.id)} title="點一下關閉">
          <b>{t.title}</b>
          <span>{t.body}</span>
        </button>
      ))}
    </div>
  )
}
