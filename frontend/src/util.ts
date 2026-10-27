// 時間與格式工具。後端時間字串為 "YYYY-MM-DDTHH:MM"（現場當地時間，無時區），
// 前端一律當成 UTC 處理，避免瀏覽器時區造成位移。

export const HOUR = 3_600_000

export const toMs = (s: string): number => Date.parse(s.length === 16 ? s + ':00Z' : s)

export const toApi = (ms: number): string => new Date(ms).toISOString().slice(0, 16)

const pad = (n: number) => String(n).padStart(2, '0')

export const fmtTime = (ms: number, withHour = true): string => {
  const d = new Date(ms)
  const date = `${d.getUTCMonth() + 1}/${d.getUTCDate()}`
  return withHour ? `${date} ${pad(d.getUTCHours())}:00` : date
}

export const fmtNum = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits)

export const LEVEL_NAMES = ['正常', '警戒', '危險', '緊急']

export const levelVar = (lvl: number): string =>
  lvl < 0 ? 'var(--lvl-none)' : `var(--lvl-${Math.min(3, lvl)})`

export const modeClass = (mode: string): string =>
  mode === '自動執行' ? 'mode-auto' : mode === 'Fallback' ? 'mode-fallback' : 'mode-manual'

// 每個場域在圖表上要畫的欄位與顯示名稱
export const SITE_CHART: Record<string, { vars: string[]; labels: Record<string, string>; unit: string; fcVar: string }> = {
  sheep: { vars: ['thi'], labels: { thi: '成羊區 THI' }, unit: '', fcVar: 'thi' },
  expert: { vars: ['do1', 'do2', 'do3'], labels: { do1: 'DO1', do2: 'DO2', do3: 'DO3' }, unit: 'mg/L', fcVar: 'do1' },
  river: { vars: ['do'], labels: { do: 'DO' }, unit: 'mg/L', fcVar: 'do' },
  breed1: { vars: ['pm25'], labels: { pm25: 'PM2.5' }, unit: 'µg/m³', fcVar: 'pm25' },
  breed1_north: { vars: ['pm25'], labels: { pm25: 'PM2.5' }, unit: 'µg/m³', fcVar: 'pm25' },
  northeast: { vars: ['pm25'], labels: { pm25: 'PM2.5' }, unit: 'µg/m³', fcVar: 'pm25' },
  site: { vars: ['pm10'], labels: { pm10: 'PM10' }, unit: 'µg/m³', fcVar: 'pm10' },
}

// 感測器欄位代碼 → 中文名稱
const SENSOR_LABELS: Record<string, string> = {
  thi: '成羊區 THI', thi_lamb: '仔羊區 THI', temp: '溫度', rh: '濕度', co2: 'CO2', nh3: 'NH3',
  temp_avg: '平均溫度', nh3_avg: '平均 NH3', pm25: 'PM2.5', pm10: 'PM10', co: 'CO', hcho: 'HCHO',
  tvoc: 'TVOC', noise: '噪音', wind: '風速', ph: 'pH', ec: 'EC', do: 'DO', wtemp: '水溫',
  battery: '電池電量', batt_v: '電池電壓', level: '水位', current: '馬達電流', power: '馬達功率',
  pf: '功率因數', heat_index: '熱指數',
}

export function sensorLabel(key: string): string {
  const m = key.match(/^(do|ph|orp|sc)(\d)$/)
  if (m) return (m[1] === 'ph' ? 'pH' : m[1].toUpperCase()) + m[2]
  return SENSOR_LABELS[key] ?? key
}
