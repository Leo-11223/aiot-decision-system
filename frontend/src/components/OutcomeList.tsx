import type { Outcome } from '../api'
import { fmtTime, toMs } from '../util'

// 執行回饋：每筆核准的目標、是否達成、現場是否真的有執行、能否計入成效樣本

const STATUS_CLASS: Record<Outcome['狀態'], string> = {
  追蹤中: 'oc-tracking',
  達成: 'oc-done',
  未達成: 'oc-missed',
  已駁回: 'oc-rejected',
  無量化目標: 'oc-none',
}

type Props = { outcomes: Outcome[]; showSite?: boolean; siteName?: (id: string) => string }

export function OutcomeList({ outcomes, showSite = false, siteName }: Props) {
  if (!outcomes.length) return <p className="empty">還沒有審核紀錄。在「現在該做什麼」核准或駁回一項決策，這裡就會開始追蹤成效。</p>
  return (
    <ul className="outcomes">
      {outcomes.map((o) => (
        <li key={o.review_id} className={STATUS_CLASS[o.狀態]}>
          <div className="oc-head">
            <span className="oc-status">{o.狀態}</span>
            <b>
              {showSite && siteName ? `${siteName(o.site_id)}：` : ''}
              {o.設備}
            </b>
          </div>
          <p className="oc-line">
            {o.動作}，{fmtTime(toMs(o.核准時間))} {o.審核 === 'approve' ? '核准' : '駁回'}
            {o.操作人 ? `（${o.操作人}）` : ''}
          </p>
          {o.目標 && (
            <p className="oc-line">
              目標：{o.目標}
              {o.期限 ? `，期限 ${fmtTime(toMs(o.期限))}` : ''}
            </p>
          )}
          <p className="oc-line">
            {o.說明}
            {o.最新值 !== undefined && o.最新值 !== null ? `，最新值 ${o.最新值}` : ''}
          </p>
          {o.現場紀錄 && (
            <p className={'oc-field ' + (o.現場執行 === false ? 'warn' : o.現場執行 ? 'ok' : '')}>
              {o.現場紀錄}
              {o.樣本 ? `：${o.樣本}` : ''}
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}
