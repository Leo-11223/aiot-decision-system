import type { Forecast } from '../api'
import { fmtNum } from '../util'

// 預測準度：每週用「到那天為止」的資料重新訓練（前 70% 訓練、後 30% 驗證），
// 並把近 7 天當時做的預測與後來的真實結果比對。回放任何時間點都只用當時已有的資料。

const SHOW_H = [1, 3, 6]
const STATUS_CLASS: Record<string, string> = { 正常: 'ok', 誤差變大: 'bad', 不如持續法: 'warn', 樣本不足: 'muted' }

const fmtDate = (s: string | null) => (s ? s.slice(5, 10).replace('-', '/') : '—')

export function ForecastAccuracy({ forecasts }: { forecasts: (Forecast | null)[] }) {
  const list = forecasts.filter((f): f is Forecast => !!f && !!f.準度追蹤)
  if (!list.length) return <p className="empty">此場域沒有預測項目。</p>
  return (
    <div className="accuracy">
      {list.map((f) => {
        const v = f.模型版本
        const rows = (f.準度追蹤 ?? []).filter((r) => SHOW_H.includes(r.時距))
        return (
          <div key={f.變數} className="acc-block">
            <div className="acc-head">
              <b>{f.變數}</b>
              <span className="acc-method">{f.方法}</span>
            </div>
            <p className="meta">
              {v && v.重訓次數 > 0
                ? `第 ${v.重訓次數} 次重訓（${fmtDate(v.重訓時間)}，用 ${v.訓練資料時數} 小時資料：前 70% 訓練、後 30% 驗證）；下次重訓 ${fmtDate(v.下次重訓)}`
                : `資料不足，尚未訓練模型（目前 ${v?.訓練資料時數 ?? 0} 小時），先用持續法；第一次重訓 ${fmtDate(v?.下次重訓 ?? null)}`}
            </p>
            <table>
              <thead>
                <tr>
                  <th>預測多久後</th>
                  <th>重訓時驗證誤差</th>
                  <th>近 7 天實際誤差</th>
                  <th>持續法近 7 天</th>
                  <th>狀態</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.時距}>
                    <td>{r.時距} 小時</td>
                    <td>{r.訓練時誤差 === null ? '—' : fmtNum(r.訓練時誤差, 2)}</td>
                    <td>{r.近期誤差 === null ? '—' : fmtNum(r.近期誤差, 2)}</td>
                    <td>{r.近期持續法誤差 === null ? '—' : fmtNum(r.近期持續法誤差, 2)}</td>
                    <td>
                      <span className={'acc-status ' + (STATUS_CLASS[r.狀態] ?? '')}>{r.狀態}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      })}
      <p className="hint">
        誤差為平均絕對誤差（單位同數值）。持續法＝「未來等於現在」。近期誤差比驗證時大 1.5 倍以上標「誤差變大」，代表模型可能需要提早重訓。
      </p>
    </div>
  )
}
