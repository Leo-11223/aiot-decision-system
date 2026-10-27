import { useState } from 'react'
import type { Decision } from '../api'
import { modeClass } from '../util'

// 標準決策資料：設備／動作／時間／幅度／理由／預期效果／風險／人工確認／Fallback
// 「人工確認」的決策可以核准或駁回，結果寫入後端資料庫（執行回饋的來源）。

type Props = {
  decisions: Decision[]
  onReview: (d: Decision, action: 'approve' | 'reject', note?: string) => Promise<void>
}

export function DecisionList({ decisions, onReview }: Props) {
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
            {d.執行模式 === '自動執行' && <span className="note">低風險且已驗證，系統自動下發</span>}
            {d.執行模式 !== '自動執行' && d.審核 && (
              <span className={'note ' + (d.審核.action === 'approve' ? 'ok' : 'bad')}>
                {d.審核.action === 'approve' ? '已核准' : '已駁回'}（{d.審核.operator ?? '—'}，{d.審核.at.replace('T', ' ').slice(5, 16)}）
              </span>
            )}
            {d.執行模式 !== '自動執行' && (
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
