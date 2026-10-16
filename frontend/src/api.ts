// 後端 API 呼叫與資料型別。後端預設跑在 http://127.0.0.1:8000
// 要改位址：在 frontend 資料夾建立 .env 檔，寫入 VITE_API_URL=http://你的位址

export const API_BASE: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'

export type Range = { start: string; end: string }

export type SiteSummary = {
  site_id: string
  name: string
  domain: string
  at: string
  range: Range
  指數: string
  數值: number | null
  等級: string
  等級碼: number
  感測可信度: number
  決策數: number
  工單數: number
  待人工確認: number
}

export type Review = { action: 'approve' | 'reject'; operator: string | null; at: string }

export type Decision = {
  id: string
  site_id: string
  場域: string
  設備: string
  動作: string
  時間: string
  幅度: string
  理由: string
  預期效果: string
  風險: string
  感測可信度: number | null
  成效已驗證: boolean
  執行模式: '自動執行' | '人工確認' | 'Fallback'
  人工確認: boolean
  Fallback: string
  成本: string
  優先: number
  審核: Review | null
}

export type WorkOrderStatus = { status: string; operator: string | null; updated_at: string | null }

export type WorkOrder = {
  id: string
  site_id: string
  場域: string
  工單: string
  類別: string
  說明: string
  嚴重度: number
  優先分數: number
  備品: string
  期限: string
  狀態: WorkOrderStatus
}

export type Diagnosis = { 問題: string; 可能原因: string; 判斷證據: string; 可信度: string }

export type Forecast = {
  變數: string
  方法: string
  現值: number | null
  預測: (number | null)[]
  下界: (number | null)[]
  上界: (number | null)[]
  改善率: number
  單位: string
}

export type Snapshot = {
  site_id: string
  name: string
  domain: string
  at: string
  range: Range
  status: {
    指數: string
    數值: number | null
    等級: string
    等級碼: number
    時間: string
    感測可信度: number
    讀值: Record<string, number | null>
  }
  diagnoses: Diagnosis[]
  decisions: Decision[]
  work_orders: WorkOrder[]
  forecast: (Forecast | null)[]
  trust_now: Record<string, number>
}

export type TimelineEvent = {
  type: '緊急' | '危險' | '異常' | '感測器'
  start: string
  end: string
  hours: number
  detail?: string
}

export type Timeline = {
  site_id: string
  start: string
  end: string
  label: string
  unit: string
  cuts: number[]
  lower_is_bad: boolean
  value: (number | null)[]
  level: number[]
  events: TimelineEvent[]
}

export type Series = {
  site_id: string
  at: string
  step: number
  t: string[]
  values: Record<string, (number | null)[]>
  min?: Record<string, (number | null)[]>
  max?: Record<string, (number | null)[]>
  trust: Record<string, (number | null)[]>
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let detail = `${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      /* 非 JSON 回應 */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

const q = (params: Record<string, string | number | undefined>) => {
  const s = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&')
  return s ? `?${s}` : ''
}

export const api = {
  health: () => request<{ status: string }>('/api/health'),
  sites: (at?: string) => request<SiteSummary[]>('/api/sites' + q({ at })),
  snapshot: (id: string, at?: string) => request<Snapshot>(`/api/sites/${id}` + q({ at })),
  timeline: (id: string) => request<Timeline>(`/api/sites/${id}/timeline`),
  series: (id: string, vars: string[], hours: number, step: number, at?: string) =>
    request<Series>(`/api/sites/${id}/series` + q({ vars: vars.join(','), hours, step, at })),
  review: (decisionId: string, action: 'approve' | 'reject', operator: string, note?: string) =>
    request<{ review_id: number }>(`/api/decisions/${decisionId}/review`, {
      method: 'POST',
      body: JSON.stringify({ action, operator, note }),
    }),
  setWorkOrder: (woId: string, status: string, operator: string) =>
    request<WorkOrderStatus>(`/api/work-orders/${woId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status, operator }),
    }),
}
