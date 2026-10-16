import { useEffect, useState } from 'react'
import { api } from '../api'
import type { SiteSummary, Timeline } from '../api'
import { TimeTape } from '../components/TimeTape'
import { fmtTime, levelVar, toMs } from '../util'

// 總覽：7 個場域各一列，左側是狀態，右側是整段期間的等級膠帶。

type Props = { onOpen: (siteId: string) => void }

export function Overview({ onOpen }: Props) {
  const [sites, setSites] = useState<SiteSummary[] | null>(null)
  const [timelines, setTimelines] = useState<Record<string, Timeline>>({})
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .sites()
      .then((s) => {
        if (!alive) return
        setSites(s)
        s.forEach((site) =>
          api
            .timeline(site.site_id)
            .then((tl) => alive && setTimelines((m) => ({ ...m, [site.site_id]: tl })))
            .catch(() => undefined),
        )
      })
      .catch((e: Error) => alive && setError(e.message))
    return () => {
      alive = false
    }
  }, [])

  if (error) return <p className="error">讀取失敗：{error}</p>
  if (!sites) return <p className="empty">載入場域中…</p>

  const severe = sites.filter((s) => s.等級碼 >= 2).length
  const pending = sites.reduce((n, s) => n + s.待人工確認, 0)
  const sorted = [...sites].sort((a, b) => b.等級碼 - a.等級碼 || a.感測可信度 - b.感測可信度)

  return (
    <div className="overview">
      <div className="overview-head">
        <h1>
          {severe > 0 ? `${severe} 個場域處於危險以上` : '所有場域正常'}
          ，{pending} 項決策等待人工確認
        </h1>
        <p className="sub">各場域以最後一筆資料為「當下」。點一列進入場域，用時間拉條回放任一時間點。</p>
      </div>

      <ol className="site-list">
        {sorted.map((s) => (
          <li key={s.site_id}>
            <button className="site-row" onClick={() => onOpen(s.site_id)} style={{ borderLeftColor: levelVar(s.等級碼) }}>
              <div className="row-name">
                <b>{s.name}</b>
                <span>{s.domain}</span>
              </div>
              <div className="row-value">
                <span className="row-index">{s.指數}</span>
                <span className="row-number" style={{ color: levelVar(s.等級碼) }}>
                  {s.數值 === null ? '—' : s.數值}
                </span>
                <span className="row-level" style={{ background: levelVar(s.等級碼) }}>
                  {s.等級}
                </span>
              </div>
              <div className="row-meta">
                <span>感測可信度 {s.感測可信度}</span>
                <span className={s.待人工確認 ? 'pending' : ''}>待確認 {s.待人工確認}</span>
                <span>工單 {s.工單數}</span>
              </div>
              <div className="row-tape">
                {timelines[s.site_id] ? (
                  <TimeTape timeline={timelines[s.site_id]} cursor={toMs(s.at)} height={18} compact />
                ) : (
                  <div className="tape placeholder" style={{ height: 18 }} />
                )}
                <div className="tape-ends">
                  <span>{fmtTime(toMs(s.range.start), false)}</span>
                  <span>{fmtTime(toMs(s.range.end), false)}</span>
                </div>
              </div>
            </button>
          </li>
        ))}
      </ol>

      <p className="legend-line">
        <i style={{ background: 'var(--lvl-0)' }} />正常
        <i style={{ background: 'var(--lvl-1)' }} />警戒
        <i style={{ background: 'var(--lvl-2)' }} />危險
        <i style={{ background: 'var(--lvl-3)' }} />緊急
        <i style={{ background: 'var(--lvl-none)' }} />無可信資料
      </p>
    </div>
  )
}
