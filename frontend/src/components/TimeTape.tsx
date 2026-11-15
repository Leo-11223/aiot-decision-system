import { useMemo } from 'react'
import type { Timeline, TimelineEvent } from '../api'
import { HOUR, fmtTime, levelVar, toMs } from '../util'
import { useWidth } from './useWidth'

// 時間膠帶：整段期間依「每小時等級」上色，上方標出事件，游標標示「當下時間」。
// 點一下膠帶就跳到那個時間。

export const EVENT_TYPES: TimelineEvent['type'][] = ['緊急', '危險', '異常', '感測器']

const EVENT_COLOR: Record<TimelineEvent['type'], string> = {
  緊急: 'var(--lvl-3)',
  危險: 'var(--lvl-2)',
  異常: 'var(--accent)',
  感測器: 'var(--muted)',
}

type Props = {
  timeline: Timeline
  cursor?: number
  windowStart?: number
  showEvents?: Set<string>
  height?: number
  compact?: boolean
  onPick?: (ms: number) => void
  hideFuture?: boolean   // 即時模式：遮住當下時間之後（未來）的等級與事件
}

export function TimeTape({ timeline, cursor, windowStart, showEvents, height = 34, compact = false, onPick, hideFuture = false }: Props) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const start = toMs(timeline.start)
  const end = toMs(timeline.end)
  const span = Math.max(HOUR, end - start)
  const x = (ms: number) => ((ms - start) / span) * width
  const evH = compact ? 0 : 10
  const barY = evH + (compact ? 0 : 4)
  const barH = height - barY

  // 連續相同等級合併成一段，減少 SVG 元素
  const runs = useMemo(() => {
    const out: { from: number; to: number; lvl: number }[] = []
    timeline.level.forEach((lvl, i) => {
      const last = out[out.length - 1]
      if (last && last.lvl === lvl && last.to === i) last.to = i + 1
      else out.push({ from: i, to: i + 1, lvl })
    })
    return out
  }, [timeline])

  const events = compact
    ? []
    : timeline.events
        .filter((e) => !showEvents || showEvents.has(e.type))
        .filter((e) => !hideFuture || cursor === undefined || toMs(e.start) <= cursor)
        // 進行中的事件只畫到當下
        .map((e) => (hideFuture && cursor !== undefined && toMs(e.end) > cursor ? { ...e, end: new Date(cursor).toISOString() } : e))

  const pick = (clientX: number) => {
    const el = ref.current
    if (!el || !onPick) return
    const r = el.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width))
    onPick(start + Math.round((ratio * span) / HOUR) * HOUR)
  }

  return (
    <div
      ref={ref}
      className={'tape' + (onPick ? ' tape-clickable' : '')}
      style={{ height }}
      onClick={(e) => pick(e.clientX)}
      role={onPick ? 'slider' : 'img'}
      aria-label={`${timeline.label} 時間軸`}
      aria-valuemin={onPick ? 0 : undefined}
      aria-valuemax={onPick ? 100 : undefined}
      aria-valuenow={onPick && cursor ? Math.round(((cursor - start) / span) * 100) : undefined}
    >
      <svg width={width} height={height} aria-hidden="true">
        {runs.map((r, i) => (
          <rect
            key={i}
            x={(r.from / timeline.level.length) * width}
            y={barY}
            width={Math.max(0.5, ((r.to - r.from) / timeline.level.length) * width)}
            height={barH}
            fill={levelVar(r.lvl)}
            opacity={r.lvl < 0 ? 0.5 : 0.85}
          />
        ))}
        {cursor !== undefined && x(cursor + HOUR) < width && (
          <rect
            x={x(cursor + HOUR)}
            y={barY}
            width={width - x(cursor + HOUR)}
            height={barH}
            fill="var(--bg)"
            opacity={hideFuture ? 1 : 0.45}
          >
            <title>{hideFuture ? '未來（即時模式下隱藏）' : '當下時間之後（回放時可預覽）'}</title>
          </rect>
        )}
        {windowStart !== undefined && cursor !== undefined && (
          <rect
            x={x(Math.max(start, windowStart))}
            y={barY}
            width={Math.max(1, x(cursor) - x(Math.max(start, windowStart)))}
            height={barH}
            fill="var(--text)"
            opacity={0.18}
          />
        )}
        {events.map((e, i) => {
          const a = x(toMs(e.start))
          const b = Math.max(a + 2, x(toMs(e.end) + HOUR))
          return (
            <rect key={i} x={a} y={0} width={b - a} height={evH - 3} rx={1} fill={EVENT_COLOR[e.type]}>
              <title>{`${e.type}：${fmtTime(toMs(e.start))}–${fmtTime(toMs(e.end))}（${e.hours} 小時）${e.detail ? '，' + e.detail : ''}`}</title>
            </rect>
          )
        })}
        {cursor !== undefined && (
          <g>
            <line x1={x(cursor)} x2={x(cursor)} y1={0} y2={height} stroke="var(--text)" strokeWidth={2} />
            {!compact && <path d={`M${x(cursor) - 5} 0 L${x(cursor) + 5} 0 L${x(cursor)} 6 Z`} fill="var(--text)" />}
          </g>
        )}
      </svg>
    </div>
  )
}

export function eventColor(type: TimelineEvent['type']): string {
  return EVENT_COLOR[type]
}
