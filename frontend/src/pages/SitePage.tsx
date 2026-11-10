import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, feedbackApi } from '../api'
import type { Decision, Outcome, Series, Snapshot, Timeline } from '../api'
import { ControlPanel } from '../components/ControlPanel'
import { DecisionList } from '../components/DecisionList'
import { OutcomeList } from '../components/OutcomeList'
import { LineChart } from '../components/LineChart'
import type { ChartSeries } from '../components/LineChart'
import { TimeBar } from '../components/TimeBar'
import { HOUR, SITE_CHART, fmtNum, fmtTime, levelVar, sensorLabel, toApi, toMs } from '../util'

const SERIES_COLORS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)']
const WO_STATUS = ['待派工', '處理中', '已完成']
const CONTROLLED = ['sheep', 'expert', 'site'] // 有可遠端控制設備的場域（模擬控制器）

type Props = { siteId: string; initialAt?: string; onAtChange: (at: string) => void }

export function SitePage({ siteId, initialAt, onAtChange }: Props) {
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [step, setStep] = useState(1)
  const [windowH, setWindowH] = useState(72)
  const [speed, setSpeed] = useState(500)
  const [playing, setPlaying] = useState(false)
  const [showEvents, setShowEvents] = useState<Set<string>>(new Set(['緊急', '危險', '感測器']))
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [series, setSeries] = useState<Series | null>(null)
  const [outcomes, setOutcomes] = useState<Outcome[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [cmdKey, setCmdKey] = useState(0)
  const reqId = useRef(0)

  const chart = SITE_CHART[siteId]
  const end = timeline ? toMs(timeline.end) : 0
  const minCursor = timeline ? toMs(timeline.start) + 72 * HOUR : 0

  // 1. 載入整段時間軸，決定起始時間
  useEffect(() => {
    let alive = true
    setTimeline(null)
    setSnap(null)
    setSeries(null)
    setPlaying(false)
    api
      .timeline(siteId)
      .then((tl) => {
        if (!alive) return
        setTimeline(tl)
        const lo = toMs(tl.start) + 72 * HOUR
        const hi = toMs(tl.end)
        const want = initialAt ? toMs(initialAt) : hi
        setCursor(Math.min(hi, Math.max(lo, Number.isNaN(want) ? hi : want)))
      })
      .catch((e: Error) => alive && setError(e.message))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [siteId])

  // 2. 當下時間改變 → 稍等一下（拖動停止）再向後端要快照與曲線
  const aggStep = windowH > 200 ? 6 : 1
  const load = useCallback(
    async (at: number) => {
      const id = ++reqId.current
      setLoading(true)
      try {
        const atStr = toApi(at)
        const [s, ser, oc] = await Promise.all([
          api.snapshot(siteId, atStr),
          api.series(siteId, chart.vars, windowH, aggStep, atStr),
          feedbackApi.outcomes(siteId, atStr),
        ])
        if (id !== reqId.current) return // 已有更新的請求，丟掉舊結果
        setSnap(s)
        setSeries(ser)
        setOutcomes(oc)
        setError(null)
      } catch (e) {
        if (id === reqId.current) setError((e as Error).message)
      } finally {
        if (id === reqId.current) setLoading(false)
      }
    },
    [siteId, chart.vars, windowH, aggStep],
  )

  useEffect(() => {
    if (cursor === null) return
    const delay = playing ? 0 : 150
    const t = window.setTimeout(() => load(cursor), delay)
    onAtChange(toApi(cursor))
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, load])

  // 3. 播放：每隔一段時間往後走一個刻度
  useEffect(() => {
    if (!playing || !timeline) return
    const t = window.setInterval(() => {
      setCursor((c) => {
        if (c === null) return c
        const next = c + step * HOUR
        if (next >= end) {
          setPlaying(false)
          return end
        }
        return next
      })
    }, speed)
    return () => window.clearInterval(t)
  }, [playing, speed, step, end, timeline])

  const onReview = async (d: Decision, action: 'approve' | 'reject', note?: string) => {
    await api.review(d.id, action, note)
    setCmdKey((k) => k + 1)
    if (cursor !== null) await load(cursor)
  }

  const onWorkOrder = async (id: string, status: string) => {
    try {
      await api.setWorkOrder(id, status)
      if (cursor !== null) await load(cursor)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const chartSeries: ChartSeries[] = useMemo(() => {
    if (!series) return []
    return chart.vars.map((v, i) => ({
      key: v,
      label: chart.labels[v],
      color: SERIES_COLORS[i % SERIES_COLORS.length],
      values: series.values[v] ?? [],
      min: series.min?.[v],
      max: series.max?.[v],
    }))
  }, [series, chart])

  const fc = snap?.forecast.find((f) => f !== null) ?? null
  const chartForecast = fc ? { label: chart.labels[chart.fcVar], values: fc.預測, lo: fc.下界, hi: fc.上界 } : null

  if (error && !timeline) return <p className="error">讀取失敗：{error}</p>
  if (!timeline || cursor === null) return <p className="empty">載入時間軸中…</p>

  const st = snap?.status
  const lowTrust = snap ? Object.entries(snap.trust_now).sort((a, b) => a[1] - b[1]).slice(0, 4) : []

  return (
    <div className="site">
      <div className="site-head">
        <div>
          <h1>{snap?.name ?? siteId}</h1>
          <p className="sub">
            {snap?.domain} 資料期間 {fmtTime(toMs(timeline.start), false)} 至 {fmtTime(end, false)}
          </p>
        </div>
        {loading && <span className="loading">更新中</span>}
      </div>

      <TimeBar
        timeline={timeline}
        cursor={cursor}
        minCursor={minCursor}
        onCursor={setCursor}
        step={step}
        onStep={setStep}
        windowH={windowH}
        onWindow={setWindowH}
        speed={speed}
        onSpeed={setSpeed}
        playing={playing}
        onPlaying={setPlaying}
        showEvents={showEvents}
        onToggleEvent={(t) =>
          setShowEvents((s) => {
            const n = new Set(s)
            if (n.has(t)) n.delete(t)
            else n.add(t)
            return n
          })
        }
      />

      {error && <p className="error">讀取失敗：{error}</p>}

      <div className="site-grid">
        <div className="col-main">
          <section className="status" style={{ borderLeftColor: st ? levelVar(st.等級碼) : undefined }}>
            <div className="status-main">
              <span className="status-index">{st?.指數 ?? timeline.label}</span>
              <span className="status-value" style={{ color: st ? levelVar(st.等級碼) : undefined }}>
                {st ? (st.數值 === null ? '—' : String(st.數值)) : '—'}
              </span>
              <span className="status-level" style={{ background: st ? levelVar(st.等級碼) : undefined }}>
                {st?.等級 ?? '—'}
              </span>
            </div>
            <div className="readings">
              {st &&
                Object.entries(st.讀值)
                  .slice(0, 6)
                  .map(([k, v]) => (
                    <div key={k}>
                      <span>{k}</span>
                      <b>{typeof v === 'number' ? fmtNum(v, Math.abs(v) < 10 ? 2 : 1) : '—'}</b>
                    </div>
                  ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2>
                {chart.vars.map((v) => chart.labels[v]).join('、')}
                <span className="unit">{chart.unit}</span>
              </h2>
              <p className="hint">
                往前 {windowH >= 168 ? `${windowH / 24} 天` : `${windowH} 小時`}
                {aggStep > 1 ? `，每 ${aggStep} 小時彙整` : ''}
                {fc ? `；虛線為未來 6 小時預測（${fc.方法}）` : ''}
              </p>
            </div>
            {series ? (
              <LineChart
                t={series.t}
                series={chartSeries}
                forecast={chartForecast}
                cursor={cursor}
                stepH={aggStep}
                cuts={timeline.cuts}
                unit={chart.unit}
              />
            ) : (
              <div className="chart placeholder" />
            )}
          </section>

          <section className="panel">
            <h2>異常診斷</h2>
            {snap && snap.diagnoses.length === 0 && <p className="empty">這個時間點沒有偵測到需要說明的異常。</p>}
            <ul className="diagnoses">
              {snap?.diagnoses.map((d) => (
                <li key={d.問題}>
                  <div className="diag-title">
                    <b>{d.問題}</b>
                    <span className={'conf conf-' + d.可信度}>可信度{d.可信度}</span>
                  </div>
                  <p>{d.可能原因}</p>
                  <p className="evidence">{d.判斷證據}</p>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <aside className="col-side">
          <section className="panel">
            <h2>現在該做什麼</h2>
            {snap ? <DecisionList decisions={snap.decisions} onReview={onReview} /> : <p className="empty">載入中</p>}
          </section>

          <section className="panel">
            <h2>執行（控制指令）</h2>
            <ControlPanel siteId={siteId} refreshKey={`${cmdKey}-${snap?.at ?? ''}`} hasController={CONTROLLED.includes(siteId)} />
          </section>

          <section className="panel">
            <h2>執行回饋</h2>
            <p className="hint">依目前的時間點評估。按播放往後走，可以看到追蹤中的核准變成達成或未達成。</p>
            <OutcomeList outcomes={outcomes} />
          </section>

          <section className="panel">
            <h2>工單</h2>
            {snap && snap.work_orders.length === 0 && <p className="empty">目前沒有工單。</p>}
            <ul className="workorders">
              {snap?.work_orders
                .slice()
                .sort((a, b) => b.優先分數 - a.優先分數)
                .map((w) => (
                  <li key={w.id}>
                    <div className="wo-title">
                      <b>{w.工單}</b>
                      <select value={w.狀態.status} onChange={(e) => onWorkOrder(w.id, e.target.value)} aria-label={`${w.工單} 狀態`}>
                        {WO_STATUS.map((s) => (
                          <option key={s}>{s}</option>
                        ))}
                      </select>
                    </div>
                    <p>{w.說明}</p>
                    <p className="meta">
                      {w.類別}，優先分數 {w.優先分數}，期限 {w.期限}
                      {w.備品 !== '—' ? `，備品：${w.備品}` : ''}
                    </p>
                  </li>
                ))}
            </ul>
          </section>

          <section className="panel">
            <h2>感測器可信度</h2>
            <ul className="trust">
              {lowTrust.map(([k, v]) => (
                <li key={k}>
                  <span>{sensorLabel(k)}</span>
                  <i>
                    <b style={{ width: `${v}%`, background: v < 50 ? 'var(--lvl-3)' : v < 80 ? 'var(--lvl-1)' : 'var(--lvl-0)' }} />
                  </i>
                  <em>{Math.round(v)}</em>
                </li>
              ))}
            </ul>
            <p className="hint">列出近 6 小時可信度最低的感測器；低於 50 的讀值不會被決策採用。</p>
          </section>
        </aside>
      </div>
    </div>
  )
}
