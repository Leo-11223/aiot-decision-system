import { useState } from 'react'
import type { Decision } from '../api'
import { modeClass } from '../util'

// 標準決策資料：設備／動作／時間／幅度／理由／預期效果／風險／人工確認／Fallback
// 「人工確認」的決策可以核准或駁回，結果寫入後端資料庫（執行回饋的來源）。

type Props = {
  decisions: Decision[]
  replay?: boolean
  onReview: (d: Decision, action: 'approve' | 'reject', note?: string) => Promise<void>
}

export function DecisionList({ decisions, onReview, replay = false }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [rejecting, setRejecting] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const sorted = [...decisions].sort((a, b) => a.優先 - b.優先)

  const act = async (d: Decision, action: 'approve' | 'reject', note?: string) => {
    setBusy(d.id + action)
    setError(null)
    try {
      await onReview(d, action, note)
      setRejecting(null)
      setReason('')
    } catch (e) {
      setError(`沒有送出：${(e as Error).message}`)
    } finally {
      setBusy(null)
    }
  }

  if (!sorted.length) return <p className="empty">這個時間點沒有需要處理的決策。</p>

  return (
    <div className="decisions">
      {error && <p className="error">{error}</p>}
      {sorted.map((d) => (
        <article key={d.id} className={'decision ' + modeClass(d.執行模式)}>
          <header>
            <span className={'mode ' + modeClass(d.執行模式)}>{d.執行模式}</span>
            <h4>
              {d.設備}
              <span className="action">{d.動作}</span>
            </h4>
          </header>
          {d.一句話 && <p className="plain">{d.一句話}</p>}
          {d.人員回饋 && <p className="feedback-flag">人員回饋：{d.人員回饋}</p>}
          <dl>
            <div>
              <dt>理由</dt>
              <dd>{d.理由}</dd>
            </div>
            <div>
              <dt>時間</dt>
              <dd>{d.時間}</dd>
            </div>
            {d.幅度 !== '—' && (
              <div>
                <dt>幅度</dt>
                <dd>{d.幅度}</dd>
              </div>
            )}
            {d.預期效果 !== '—' && (
              <div>
                <dt>預期效果</dt>
                <dd>{d.預期效果}</dd>
              </div>
            )}
            {d.控制指令 && (
              <div>
                <dt>控制指令</dt>
                <dd>
                  <span className={'ctl-kind ' + (d.控制指令.類型 === '設備指令' ? 'device' : '')}>{d.控制指令.類型}</span>
                  {d.控制指令.類型 === '設備指令'
                    ? `${d.控制指令.協定}：` + (d.控制指令.寫入 ?? []).map((w) => `FC${w.功能碼} ${w.位址}＝${w.值}（${w.點位}）`).join('；')
                    : d.控制指令.說明}
                </dd>
              </div>
            )}
            <div>
              <dt>風險</dt>
              <dd>
                {d.風險}風險，感測可信度 {d.感測可信度 ?? '—'}，成效{d.成效已驗證 ? '已驗證' : '尚未驗證'}
              </dd>
            </div>
            {d.Fallback !== '—' && (
              <div>
                <dt>Fallback</dt>
                <dd>{d.Fallback}</dd>
              </div>
            )}
            {d.成本 !== '—' && (
              <div>
                <dt>成本</dt>
                <dd>{d.成本}</dd>
              </div>
            )}
          </dl>
          <footer>
            {d.執行模式 === '維持現狀' && <span className="note">不需動作，持續監測</span>}
            {d.執行模式 === '自動執行' && (
              <span className="note">
                {d.控制指令?.類型 === '設備指令'
                  ? replay
                    ? '低風險且已驗證；回放過去時間點只顯示，不實際下發'
                    : '低風險且已驗證，系統自動下發（見「執行」面板）'
                  : '低風險且已驗證，系統自動處理'}
              </span>
            )}
            {d.執行模式 === '自動執行' && (
              <div className="review">
                {d.審核?.action === 'reject' && (
                  <span className="note bad">已駁回（{d.審核.operator ?? '—'}）</span>
                )}
                <button
                  disabled={busy !== null}
                  onClick={() => setRejecting(rejecting === d.id ? null : d.id)}
                  aria-expanded={rejecting === d.id}
                  title="現場不同意這項自動執行：駁回會撤回指令；同一規則常被駁回會自動改回人工確認"
                >
                  駁回自動執行
                </button>
              </div>
            )}
            {d.延續 && !d.審核 && (
              <span className={'note ' + (d.延續.類型 === '延續核准' ? 'ok' : 'bad')}>
                {d.延續.類型 === '延續核准'
                  ? `沿用 ${d.延續.依據時間.replace('T', ' ').slice(5, 16)} 的核准（${d.延續.操作人 ?? '—'}），同一段事件內不需重按；${d.延續.到期.replace('T', ' ').slice(5, 16)} 前有效`
                  : `已於 ${d.延續.依據時間.replace('T', ' ').slice(5, 16)} 駁回（${d.延續.操作人 ?? '—'}${d.延續.原因 ? '：' + d.延續.原因 : ''}），${d.延續.到期.replace('T', ' ').slice(5, 16)} 前不再提醒`}
              </span>
            )}
            {(d.執行模式 === '人工確認' || d.執行模式 === 'Fallback') && d.審核 && (
              <span className={'note ' + (d.審核.action === 'approve' ? 'ok' : 'bad')}>
                {d.審核.action === 'approve' ? '已核准' : '已駁回'}（{d.審核.operator ?? '—'}，{d.審核.at.replace('T', ' ').slice(5, 16)}）
              </span>
            )}
            {(d.執行模式 === '人工確認' || d.執行模式 === 'Fallback') && (
              <div className="review">
                <button className="primary" disabled={busy !== null} onClick={() => act(d, 'approve')}>
                  {busy === d.id + 'approve' ? '送出中' : d.審核 ? '重新核准' : '核准'}
                </button>
                <button disabled={busy !== null} onClick={() => setRejecting(rejecting === d.id ? null : d.id)} aria-expanded={rejecting === d.id}>
                  駁回
                </button>
              </div>
            )}
            {rejecting === d.id && (
              <form
                className="reject-form"
                onSubmit={(e) => {
                  e.preventDefault()
                  act(d, 'reject', reason.trim() || undefined)
                }}
              >
                <input
                  autoFocus
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="駁回原因，例如：現場有雨、設備維修中"
                  aria-label="駁回原因"
                />
                <button type="submit" disabled={busy !== null}>
                  {busy === d.id + 'reject' ? '送出中' : '確認駁回'}
                </button>
              </form>
            )}
          </footer>
        </article>
      ))}
    </div>
  )
}
