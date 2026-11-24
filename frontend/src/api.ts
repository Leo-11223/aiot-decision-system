// 後端 API 呼叫與資料型別。
// 開發時（npm run dev）後端在 http://127.0.0.1:8000；部署時前後端同網域，用相對路徑。
// 要改位址：在 frontend 資料夾建立 .env 檔，寫入 VITE_API_URL=http://你的位址

export const API_BASE: string = import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

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
  執行模式: '自動執行' | '人工確認' | 'Fallback' | '維持現狀'
  人工確認: boolean
  Fallback: string
  成本: string
  優先: number
  審核: Review | null
  延續?: { 類型: '延續核准' | '駁回冷卻'; 依據時間: string; 操作人: string | null; 到期: string; 原因?: string | null }
  事件開始?: string
  一句話?: string
  人員回饋?: string
  控制指令: ControlPlan
}

// ---------- 05 執行：控制指令（模擬控制器） ----------
export type ModbusWrite = { 功能碼: string; 功能: string; 位址: string; 值: string; 點位: string }

export type ControlPlan = {
  類型: '設備指令' | '系統設定' | '系統通知' | '人工作業' | '維持現狀'
  說明: string
  協定?: string
  控制器?: string
  站號?: number
  點位來源?: string
  寫入?: ModbusWrite[]
}

export type Command = {
  id: number
  site_id: string
  decision_id: string | null
  data_time: string | null
  device: string
  action: string
  plan: ControlPlan
  status: '已下發' | '設備已執行' | '下發失敗' | '人工已完成' | '已撤回'
  operator: string | null
  auto: boolean
  note: string | null
  sent_at: string
  updated_at: string
  done_by: string | null
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
  模型MAE?: number[]
  持續法MAE?: number[]
  模型版本?: ModelVersion | null
  準度追蹤?: Tracking[] | null
}

// 每週擴展視窗重訓：目前使用第幾次重訓的模型
export type ModelVersion = {
  重訓次數: number
  重訓時間: string | null
  訓練資料時數: number
  下次重訓: string | null
  每幾天重訓: number
}

// 預測準度追蹤：近 7 天「當時的預測」與後來真實結果的誤差
export type Tracking = {
  時距: number
  近期誤差: number | null
  近期持續法誤差: number | null
  訓練時誤差: number | null
  樣本: number
  狀態: '正常' | '誤差變大' | '不如持續法' | '樣本不足'
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
    資料時間?: string | null
    '資料延遲(h)'?: number | null
  }
  diagnoses: Diagnosis[]
  decisions: Decision[]
  work_orders: WorkOrder[]
  forecast: (Forecast | null)[]
  trust_now: Record<string, number>
  回放模式?: boolean
  白話?: { 現在: string; 接下來: string; 該做什麼: string }
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

// ---------- 登入憑證 ----------
const TOKEN_KEY = 'aiot-token'
let memToken: string | null = null

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? memToken
  } catch {
    return memToken
  }
}

export function setToken(t: string | null): void {
  memToken = t
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 無法儲存時只放在記憶體 */
  }
}

// 任何請求收到 401（未登入或已過期）都會觸發這個事件，App 收到後回到登入頁
export const UNAUTHORIZED_EVENT = 'aiot-unauthorized'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const res = await fetch(API_BASE + path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    setToken(null)
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  }
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
  health: () => request<{ status: 'ok' | 'loading' | 'error'; error: string | null }>('/api/health'),
  sites: (at?: string) => request<SiteSummary[]>('/api/sites' + q({ at })),
  snapshot: (id: string, at?: string) => request<Snapshot>(`/api/sites/${id}` + q({ at })),
  timeline: (id: string) => request<Timeline>(`/api/sites/${id}/timeline`),
  series: (id: string, vars: string[], hours: number, step: number, at?: string) =>
    request<Series>(`/api/sites/${id}/series` + q({ vars: vars.join(','), hours, step, at })),
  review: (decisionId: string, action: 'approve' | 'reject', note?: string) =>
    request<{ review_id: number; 訊息?: string | null; command?: Command | null }>(`/api/decisions/${decisionId}/review`, {
      method: 'POST',
      body: JSON.stringify({ action, note }),
    }),
  setWorkOrder: (woId: string, status: string) =>
    request<WorkOrderStatus>(`/api/work-orders/${woId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
}

export const controlApi = {
  commands: (siteId?: string, limit = 20) =>
    request<{ offline: boolean | string[]; commands: Command[] }>('/api/control/commands' + q({ site_id: siteId, limit })),
  manualDone: (id: number, note?: string) =>
    request<Command>(`/api/control/commands/${id}/manual-done`, { method: 'POST', body: JSON.stringify({ note }) }),
  setOffline: (siteId: string, offline: boolean) =>
    request<{ site_id: string; offline: boolean }>('/api/control/sim', {
      method: 'POST',
      body: JSON.stringify({ site_id: siteId, offline }),
    }),
}

export type Outcome = {
  review_id: number
  decision_id: string
  site_id: string
  設備: string
  動作: string
  核准時間: string
  審核: 'approve' | 'reject'
  操作人: string | null
  備註: string | null
  狀態: '追蹤中' | '達成' | '未達成' | '已駁回' | '無量化目標'
  說明: string
  目標?: string
  期限?: string
  現場執行?: boolean | null
  現場紀錄?: string
  樣本?: string
  最新值?: number | null
  達成時間?: string
  歷時?: number
}

export type RuleStat = {
  site_id: string
  場域: string | null
  設備: string
  核准: number
  駁回: number
  駁回率: number
  建議檢討: boolean
  駁回原因: string[]
  成效?: { 動作: string; 樣本數: number; 門檻: number; 效果: number | null; 標準誤: number | null; 已驗證: boolean }
}

export type RuleReport = { rules: RuleStat[]; 門檻: { 樣本數: number; 單尾z: number; 檢討最少審核次數: number } }

export const feedbackApi = {
  outcomes: (siteId?: string, at?: string) => request<Outcome[]>('/api/feedback/outcomes' + q({ site_id: siteId, at })),
  rules: () => request<RuleReport>('/api/feedback/rules'),
}

// ---------- 帳號與管理員 ----------
export type User = { username: string; display_name: string; role: 'staff' | 'admin'; role_name: string }

export type AdminUser = User & {
  active: boolean
  created_at: string
  last_login: string | null
  counts: { approve: number; reject: number; work_order: number }
}

export type AuditEntry = {
  id: number
  ts: string
  username: string | null
  display_name: string | null
  role: string | null
  action: string
  site_id: string | null
  target: string | null
  detail: Record<string, string | number | null>
}

export const authApi = {
  login: (username: string, password: string) =>
    request<{ token: string; user: User }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  me: () => request<User>('/api/auth/me'),
  changePassword: (old_password: string, new_password: string) =>
    request<{ ok: boolean }>('/api/auth/password', { method: 'POST', body: JSON.stringify({ old_password, new_password }) }),
}

export const adminApi = {
  audit: (username?: string, action?: string) => request<AuditEntry[]>('/api/admin/audit' + q({ username, action, limit: 500 })),
  users: () => request<AdminUser[]>('/api/admin/users'),
  createUser: (body: { username: string; display_name: string; password: string; role: string }) =>
    request<{ ok: boolean }>('/api/admin/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (username: string, body: { display_name?: string; role?: string; active?: boolean; password?: string }) =>
    request<{ ok: boolean }>(`/api/admin/users/${encodeURIComponent(username)}`, { method: 'PATCH', body: JSON.stringify(body) }),
}
