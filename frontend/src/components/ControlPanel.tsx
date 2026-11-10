import { useCallback, useEffect, useState } from 'react'
import { controlApi } from '../api'
import type { Command } from '../api'

// 05 執行：核准後產生的控制指令與模擬控制器回報。
// 已下發 → 設備已執行（約 2 秒）；通訊中斷時 → 下發失敗，改派人工 → 人員回報完成。

const STATUS_CLASS: Record<Command['status'], string> = {
  已下發: 'sent',
  設備已執行: 'done',
  下發失敗: 'failed',
  人工已完成: 'manual',
}

const fmtTs = (ts: string | null) => (ts ? ts.replace('T', ' ').slice(5, 16) : '—')

export function writesText(c: { plan: Command['plan'] }) {
  return (c.plan.寫入 ?? []).map((w) => `FC${w.功能碼} ${w.位址}＝${w.值}（${w.點位}）`).join('；')
}

type Props = { siteId: string; refreshKey: string; hasController: boolean }

export function ControlPanel({ siteId, refreshKey, hasController }: Props) {
  const [cmds, setCmds] = useState<Command[]>([])
  const [offline, setOffline] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await controlApi.commands(siteId, 10)
      setCmds(r.commands)
      setOffline(r.offline === true)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [siteId])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  // 有「已下發」的指令時，每秒重新讀取，等控制器回報
  const waiting = cmds.some((c) => c.status === '已下發')
  useEffect(() => {
    if (!waiting) return
    const t = window.setTimeout(load, 1000)
    return () => window.clearTimeout(t)
  }, [waiting, cmds, load])

  const toggle = async () => {
    const want = !offline
    setOffline(want) // 先切換畫面，失敗再改回
    try {
      const r = await controlApi.setOffline(siteId, want)
      setOffline(r.offline)
    } catch (e) {
      setOffline(!want)
      setError((e as Error).message)
    }
  }

  const done = async (c: Command) => {
    setBusy(c.id)
    try {
      await controlApi.manualDone(c.id)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="control">
      {hasController ? (
        <label className="sim-toggle">
          <input type="checkbox" checked={offline} onChange={toggle} />
          模擬通訊中斷（測試下發失敗 → 改派人工）
        </label>
      ) : (
        <p className="hint">此場域沒有可遠端控制的設備，決策以系統設定、通知或人工作業執行。</p>
      )}
      {error && <p className="error">{error}</p>}
      {cmds.length === 0 && hasController && <p className="empty">尚無控制指令。核准需要動設備的決策後會出現在這裡。</p>}
      <ul className="commands">
        {cmds.map((c) => (
          <li key={c.id} className={STATUS_CLASS[c.status]}>
            <div className="cmd-title">
              <b>
                {c.device}：{c.plan.說明}
              </b>
              <span className={'cmd-status ' + STATUS_CLASS[c.status]}>{c.status}</span>
            </div>
            <p className="cmd-writes">
              {c.plan.協定}，{c.plan.控制器}（站號 {c.plan.站號}）：{writesText(c)}
            </p>
            <p className="meta">
              {c.auto ? '系統自動下發' : `${c.operator ?? '—'} 核准下發`}，{fmtTs(c.sent_at)}
              {c.data_time ? `；依據資料時間 ${c.data_time.replace('T', ' ').replace(/-/g, '/')}` : ''}
            </p>
            {c.note && <p className="meta">{c.note}</p>}
            {c.status === '下發失敗' && (
              <button disabled={busy !== null} onClick={() => done(c)}>
                {busy === c.id ? '送出中' : '回報現場已人工完成'}
              </button>
            )}
          </li>
        ))}
      </ul>
      {hasController && cmds[0]?.plan.點位來源 && <p className="hint">點位：{cmds[0].plan.點位來源}。此為模擬控制器，正式導入時改接現場 SCADA／PLC。</p>}
    </div>
  )
}
