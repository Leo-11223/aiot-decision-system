import { useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { HOUR, LEVEL_NAMES, fmtNum, fmtTime, levelVar, toMs } from '../util'
import { useWidth } from './useWidth'

// 自繪 SVG 折線圖：實測值（左）＋預測與誤差區間（右，游標之後），並畫出等級門檻線。

export type ChartSeries = {
  key: string
  label: string
  color: string
  values: (number | null)[]
  min?: (number | null)[]
  max?: (number | null)[]
}

export type ChartForecast = {
  label: string
  values: (number | null)[]
  lo: (number | null)[]
  hi: (number | null)[]
}

type Props = {
  t: string[]
  series: ChartSeries[]
  forecast?: ChartForecast | null
  cursor: number
  stepH: number
  cuts: number[]
  unit: string
  height?: number
}

const M = { top: 14, right: 64, bottom: 26, left: 44 }

function niceTicks(lo: number, hi: number, n = 5): number[] {
  const span = hi - lo || 1
  const raw = span / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) ?? mag * 10
  const out: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toFixed(6)))
  return out
}

export function LineChart({ t, series, forecast, cursor, stepH, cuts, unit, height = 260 }: Props) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)

  const times = useMemo(() => t.map(toMs), [t])
  const fcTimes = useMemo(() => (forecast ? forecast.values.map((_, i) => cursor + (i + 1) * HOUR) : []), [forecast, cursor])

  const x0 = times[0] ?? cursor - 24 * HOUR
  const x1 = Math.max(cursor, fcTimes[fcTimes.length - 1] ?? cursor)
  const iw = Math.max(10, width - M.left - M.right)
  const ih = height - M.top - M.bottom
  const X = (ms: number) => M.left + ((ms - x0) / Math.max(HOUR, x1 - x0)) * iw

  const [ylo, yhi] = useMemo(() => {
    const vals: number[] = []
    series.forEach((s) => {
      s.values.forEach((v) => v !== null && vals.push(v))
      s.min?.forEach((v) => v !== null && vals.push(v))
      s.max?.forEach((v) => v !== null && vals.push(v))
    })
    forecast?.lo.forEach((v) => v !== null && vals.push(v))
    forecast?.hi.forEach((v) => v !== null && vals.push(v))
    if (!vals.length) return [0, 1]
    let lo = Math.min(...vals)
    let hi = Math.max(...vals)
    const pad = (hi - lo) * 0.25 || 1
    // 只納入資料附近的門檻線，避免把曲線壓扁
    cuts.forEach((c) => {
      if (c >= lo - pad && c <= hi + pad) {
        lo = Math.min(lo, c)
        hi = Math.max(hi, c)
      }
    })
    const m = (hi - lo) * 0.08 || 0.5
    return [lo - m, hi + m]
  }, [series, forecast, cuts])

  const Y = (v: number) => M.top + ih - ((v - ylo) / (yhi - ylo)) * ih
  const yTicks = niceTicks(ylo, yhi, 5)

  const xTicks = useMemo(() => {
    const spanH = (x1 - x0) / HOUR
    const every = spanH <= 30 ? 3 : spanH <= 80 ? 12 : spanH <= 200 ? 24 : spanH <= 400 ? 48 : 24 * 7
    const out: number[] = []
    const first = Math.ceil(x0 / (every * HOUR)) * every * HOUR
    for (let ms = first; ms <= x1; ms += every * HOUR) out.push(ms)
    return { list: out, withHour: every < 24 }
  }, [x0, x1])

  const path = (vals: (number | null)[], xs: number[]) => {
    let d = ''
    let pen = false
    vals.forEach((v, i) => {
      if (v === null || Number.isNaN(v)) {
        pen = false
        return
      }
      d += `${pen ? 'L' : 'M'}${X(xs[i]).toFixed(1)} ${Y(v).toFixed(1)}`
      pen = true
    })
    return d
  }

  const band = (lo: (number | null)[], hi: (number | null)[], xs: number[]) => {
    const pts = xs.map((x, i) => ({ x, lo: lo[i], hi: hi[i] })).filter((p) => p.lo !== null && p.hi !== null)
    if (pts.length < 2) return ''
    const up = pts.map((p) => `${X(p.x).toFixed(1)} ${Y(p.hi as number).toFixed(1)}`)
    const dn = [...pts].reverse().map((p) => `${X(p.x).toFixed(1)} ${Y(p.lo as number).toFixed(1)}`)
    return `M${up.join('L')}L${dn.join('L')}Z`
  }

  // 預測線從最後一筆實測接起
  const lastActual = (() => {
    const s = series.find((s) => s.label === forecast?.label) ?? series[0]
    if (!s) return null
    for (let i = s.values.length - 1; i >= 0; i--) if (s.values[i] !== null) return { x: times[i], v: s.values[i] as number }
    return null
  })()

  const onMove = (e: MouseEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const ms = x0 + ((e.clientX - r.left) / r.width) * (x1 - x0)
    let best = 0
    times.forEach((tm, i) => {
      if (Math.abs(tm - ms) < Math.abs(times[best] - ms)) best = i
    })
    setHover(ms > cursor && fcTimes.length ? -1 - Math.min(fcTimes.length - 1, Math.round((ms - cursor) / HOUR) - 1) : best)
  }

  const hoverInfo = (() => {
    if (hover === null) return null
    if (hover < 0 && forecast) {
      const i = -1 - hover
      return { ms: fcTimes[i], rows: [{ label: `${forecast.label} 預測`, color: 'var(--accent)', v: forecast.values[i], extra: `${fmtNum(forecast.lo[i])}–${fmtNum(forecast.hi[i])}` }] }
    }
    if (hover >= 0)
      return {
        ms: times[hover],
        rows: series.map((s) => ({ label: s.label, color: s.color, v: s.values[hover], extra: s.max ? `${fmtNum(s.min?.[hover])}–${fmtNum(s.max?.[hover])}` : '' })),
      }
    return null
  })()

  return (
    <div ref={ref} className="chart" style={{ height }}>
      <svg width={width} height={height} role="img" aria-label={`${series.map((s) => s.label).join('、')} 時間序列`}>
        {/* 未來（預測）區域 */}
        {fcTimes.length > 0 && <rect x={X(cursor)} y={M.top} width={X(x1) - X(cursor)} height={ih} fill="var(--accent)" opacity={0.06} />}
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={M.left} x2={M.left + iw} y1={Y(v)} y2={Y(v)} stroke="var(--grid)" />
            <text x={M.left - 8} y={Y(v) + 4} textAnchor="end" className="tick">
              {v}
            </text>
          </g>
        ))}
        {xTicks.list.map((ms) => (
          <text key={ms} x={X(ms)} y={height - 8} textAnchor="middle" className="tick">
            {fmtTime(ms, xTicks.withHour)}
          </text>
        ))}
        {cuts.map((c, i) =>
          c >= ylo && c <= yhi ? (
            <g key={c}>
              <line x1={M.left} x2={M.left + iw} y1={Y(c)} y2={Y(c)} stroke={levelVar(i + 1)} strokeDasharray="5 4" />
              <text x={M.left + iw + 6} y={Y(c) + 4} className="tick" fill={levelVar(i + 1)}>
                {LEVEL_NAMES[i + 1]} {c}
              </text>
            </g>
          ) : null,
        )}
        {series.map((s) =>
          s.min && s.max ? <path key={s.key + 'b'} d={band(s.min, s.max, times)} fill={s.color} opacity={0.15} /> : null,
        )}
        {series.map((s) => (
          <path key={s.key} d={path(s.values, times)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {forecast && (
          <g>
            <path d={band(forecast.lo, forecast.hi, fcTimes)} fill="var(--accent)" opacity={0.18} />
            <path
              d={(lastActual ? `M${X(lastActual.x).toFixed(1)} ${Y(lastActual.v).toFixed(1)}` : '') + path(forecast.values, fcTimes).replace(/^M/, lastActual ? 'L' : 'M')}
              fill="none"
              stroke="var(--accent)"
              strokeWidth={2}
              strokeDasharray="6 4"
            />
          </g>
        )}
        <line x1={X(cursor)} x2={X(cursor)} y1={M.top} y2={M.top + ih} stroke="var(--text)" strokeWidth={1.5} />
        <text x={X(cursor) + 4} y={M.top + 10} className="tick">
          現在
        </text>
        {hoverInfo && <line x1={X(hoverInfo.ms)} x2={X(hoverInfo.ms)} y1={M.top} y2={M.top + ih} stroke="var(--muted)" strokeDasharray="2 3" />}
        <rect x={M.left} y={M.top} width={iw} height={ih} fill="transparent" onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
        <text x={M.left - 8} y={M.top - 4} textAnchor="end" className="tick">
          {unit}
        </text>
      </svg>
      {hoverInfo && (
        <div className="tooltip" style={{ left: Math.min(width - 190, Math.max(0, X(hoverInfo.ms) + 10)), top: 8 }}>
          <div className="tooltip-time">
            {fmtTime(hoverInfo.ms)}
            {stepH > 1 && hover !== null && hover >= 0 ? `（${stepH} 小時平均）` : ''}
          </div>
          {hoverInfo.rows.map((r) => (
            <div key={r.label} className="tooltip-row">
              <i style={{ background: r.color }} />
              <span>{r.label}</span>
              <b>{fmtNum(r.v, 2)}</b>
              {r.extra && <em>{r.extra}</em>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
