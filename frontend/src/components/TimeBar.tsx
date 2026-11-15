import type { Timeline } from '../api'
import { HOUR, fmtTime, toMs } from '../util'
import { EVENT_TYPES, TimeTape, eventColor } from './TimeTape'

// 時間控制列：可調刻度、顯示範圍、播放速度；拉條、前後一格、跳到上一個／下一個事件。

export const STEP_OPTIONS = [
  { h: 1, label: '1 小時' },
  { h: 3, label: '3 小時' },
  { h: 6, label: '6 小時' },
  { h: 24, label: '1 天' },
]
export const WINDOW_OPTIONS = [
  { h: 24, label: '1 天' },
  { h: 72, label: '3 天' },
  { h: 168, label: '1 週' },
  { h: 720, label: '30 天' },
]
export const SPEED_OPTIONS = [
  { ms: 1000, label: '慢' },
  { ms: 500, label: '中' },
  { ms: 200, label: '快' },
]

type Props = {
  timeline: Timeline
  cursor: number
  minCursor: number
  onCursor: (ms: number) => void
  step: number
  onStep: (h: number) => void
  windowH: number
  onWindow: (h: number) => void
  speed: number
  onSpeed: (ms: number) => void
  playing: boolean
  onPlaying: (p: boolean) => void
  showEvents: Set<string>
  onToggleEvent: (t: string) => void
  hideFuture?: boolean
}

export function TimeBar(p: Props) {
  const end = toMs(p.timeline.end)
  const clamp = (ms: number) => Math.min(end, Math.max(p.minCursor, ms))
  const totalH = Math.round((end - p.minCursor) / HOUR)
  const idx = Math.round((p.cursor - p.minCursor) / HOUR)

  const important = p.timeline.events.filter((e) => p.showEvents.has(e.type))
  const nextEvent = p.hideFuture ? undefined : important.find((e) => toMs(e.start) > p.cursor)
  const prevEvent = [...important].reverse().find((e) => toMs(e.start) < p.cursor)

  return (
    <section className="timebar" aria-label="時間控制">
      <div className="timebar-head">
        <div className="now">
          <span className="now-label">當下時間</span>
          <span className="now-value">{fmtTime(p.cursor)}</span>
        </div>
        <div className="controls">
          <label>
            刻度
            <select value={p.step} onChange={(e) => p.onStep(Number(e.target.value))}>
              {STEP_OPTIONS.map((o) => (
                <option key={o.h} value={o.h}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            顯示範圍
            <select value={p.windowH} onChange={(e) => p.onWindow(Number(e.target.value))}>
              {WINDOW_OPTIONS.map((o) => (
                <option key={o.h} value={o.h}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            播放速度
            <select value={p.speed} onChange={(e) => p.onSpeed(Number(e.target.value))}>
              {SPEED_OPTIONS.map((o) => (
                <option key={o.ms} value={o.ms}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <TimeTape
        timeline={p.timeline}
        cursor={p.cursor}
        windowStart={p.cursor - (p.windowH - 1) * HOUR}
        showEvents={p.showEvents}
        hideFuture={p.hideFuture}
        height={40}
        onPick={(ms) => {
          p.onPlaying(false)
          p.onCursor(clamp(ms))
        }}
      />
      <input
        className="scrubber"
        type="range"
        min={0}
        max={totalH}
        step={p.step}
        value={idx}
        aria-label="拖動調整當下時間"
        aria-valuetext={fmtTime(p.cursor)}
        onChange={(e) => {
          p.onPlaying(false)
          p.onCursor(clamp(p.minCursor + Number(e.target.value) * HOUR))
        }}
      />
      <div className="tape-ends">
        <span>{fmtTime(toMs(p.timeline.start), false)}</span>
        <span>{fmtTime(end, false)}</span>
      </div>

      <div className="timebar-foot">
        <div className="buttons">
          <button disabled={!prevEvent} onClick={() => prevEvent && p.onCursor(clamp(toMs(prevEvent.start)))} title="跳到上一個事件">
            上一個事件
          </button>
          <button onClick={() => p.onCursor(clamp(p.cursor - p.step * HOUR))}>往前 {STEP_OPTIONS.find((o) => o.h === p.step)?.label}</button>
          <button className="primary" onClick={() => p.onPlaying(!p.playing)} aria-pressed={p.playing}>
            {p.playing ? '暫停' : '播放'}
          </button>
          <button onClick={() => p.onCursor(clamp(p.cursor + p.step * HOUR))}>往後 {STEP_OPTIONS.find((o) => o.h === p.step)?.label}</button>
          <button disabled={!nextEvent} onClick={() => nextEvent && p.onCursor(clamp(toMs(nextEvent.start)))} title="跳到下一個事件">
            下一個事件
          </button>
        </div>
        <div className="legend" role="group" aria-label="顯示的事件類型">
          {EVENT_TYPES.map((t) => (
            <button
              key={t}
              className={'chip' + (p.showEvents.has(t) ? ' on' : '')}
              aria-pressed={p.showEvents.has(t)}
              onClick={() => p.onToggleEvent(t)}
            >
              <i style={{ background: eventColor(t) }} />
              {t}
            </button>
          ))}
        </div>
      </div>
    </section>
  )
}
