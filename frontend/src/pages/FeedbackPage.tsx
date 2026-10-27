import { useEffect, useState } from 'react'
import { feedbackApi } from '../api'
import type { Outcome, RuleReport } from '../api'
import { OutcomeList } from '../components/OutcomeList'
import { fmtNum } from '../util'

// 執行回饋頁：所有核准的最終成效，以及每條規則的學習狀態（核准駁回統計、成效驗證進度）

type Props = { siteName: (id: string) => string }

export function FeedbackPage({ siteName }: Props) {
  const [outcomes, setOutcomes] = useState<Outcome[] | null>(null)
  const [report, setReport] = useState<RuleReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([feedbackApi.outcomes(), feedbackApi.rules()])
      .then(([o, r]) => {
        setOutcomes(o)
        setReport(r)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <p className="error">讀取失敗：{error}</p>
  if (!outcomes || !report) return <p className="empty">載入中…</p>

  const counts = outcomes.reduce<Record<string, number>>((m, o) => ({ ...m, [o.狀態]: (m[o.狀態] ?? 0) + 1 }), {})
  const notExecuted = outcomes.filter((o) => o.現場執行 === false).length
  const flagged = report.rules.filter((r) => r.建議檢討).length

  return (
    <div className="feedback">
      <div className="overview-head">
        <h1>
          {outcomes.length} 筆審核，{counts['達成'] ?? 0} 筆達成目標，{notExecuted} 筆現場未執行
        </h1>
        <p className="sub">
          成效以各場域最後一筆資料評估。現場未執行的核准不計入成效樣本；動作需累積 {report.門檻.樣本數} 個樣本且效果顯著，才會升級為自動執行。
        </p>
      </div>

      <div className="site-grid">
        <section className="panel">
          <h2>成效追蹤</h2>
          <OutcomeList outcomes={outcomes} showSite siteName={siteName} />
        </section>

        <section className="panel">
          <h2>規則學習{flagged ? `（${flagged} 條建議檢討）` : ''}</h2>
          <ul className="rules">
            {report.rules.map((r) => {
              const e = r.成效
              const pct = e ? Math.min(100, (e.樣本數 / e.門檻) * 100) : 0
              return (
                <li key={r.site_id + r.設備} className={r.建議檢討 ? 'flag' : ''}>
                  <div className="oc-head">
                    <b>
                      {r.場域 ? `${siteName(r.site_id)}：` : `${siteName(r.site_id)}（歷史紀錄）：`}
                      {r.設備}
                    </b>
                    {r.建議檢討 && <span className="oc-status">建議檢討規則</span>}
                  </div>
                  {r.核准 + r.駁回 > 0 && (
                    <p className="oc-line">
                      核准 {r.核准}、駁回 {r.駁回}（駁回率 {Math.round(r.駁回率 * 100)}%）
                      {r.駁回原因.length ? `，原因：${r.駁回原因.slice(-3).join('；')}` : ''}
                    </p>
                  )}
                  {e ? (
                    <>
                      <div className="progress" aria-label={`成效樣本 ${e.樣本數} / ${e.門檻}`}>
                        <b style={{ width: `${pct}%`, background: e.已驗證 ? 'var(--lvl-0)' : 'var(--lvl-1)' }} />
                      </div>
                      <p className="oc-line">
                        成效樣本 {e.樣本數} / {e.門檻}，效果 {fmtNum(e.效果, 2)}
                        {e.標準誤 !== null ? ` ± ${fmtNum(e.標準誤, 2)}` : ''}：
                        {e.已驗證 ? '已驗證，可自動執行' : e.樣本數 < e.門檻 ? '樣本不足，維持人工確認' : '效果未達顯著，維持人工確認'}
                      </p>
                    </>
                  ) : (
                    <p className="oc-line muted">沒有設備紀錄可量化成效，維持人工確認。</p>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      </div>
    </div>
  )
}
