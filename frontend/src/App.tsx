import { useEffect, useState } from 'react'
import { API_BASE, api } from './api'
import { Overview } from './pages/Overview'
import { SitePage } from './pages/SitePage'
import { getOperator, setOperator } from './util'

// 以網址 # 做簡單路由：#/ 為總覽，#/site/sheep?at=2026-07-20T14:00 為場域頁（可分享、可上一頁）

const SHORT: Record<string, string> = {
  sheep: '羊舍',
  expert: '專家系統',
  river: '三爺宮溪',
  breed1: '育成一廠',
  breed1_north: '育成北側',
  northeast: '東北角',
  site: '工地',
}

type Route = { page: 'overview' } | { page: 'site'; id: string; at?: string }

function parseHash(): Route {
  const h = window.location.hash.replace(/^#/, '')
  const m = h.match(/^\/site\/([a-z0-9_]+)(?:\?at=([0-9T:-]+))?/)
  if (m && m[1] in SHORT) return { page: 'site', id: m[1], at: m[2] }
  return { page: 'overview' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash())
  const [online, setOnline] = useState<boolean | null>(null)
  const [operator, setOp] = useState(getOperator())

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // 後端啟動要約 1 分鐘：連不上時每 4 秒重試
  useEffect(() => {
    let alive = true
    let timer = 0
    const check = () =>
      api
        .health()
        .then(() => alive && setOnline(true))
        .catch(() => {
          if (!alive) return
          setOnline(false)
          timer = window.setTimeout(check, 4000)
        })
    check()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [])

  const go = (hash: string) => {
    window.location.hash = hash
  }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          AIoT 決策閉環
        </a>
        <nav aria-label="場域">
          <a href="#/" className={route.page === 'overview' ? 'active' : ''}>
            總覽
          </a>
          {Object.entries(SHORT).map(([id, name]) => (
            <a key={id} href={`#/site/${id}`} className={route.page === 'site' && route.id === id ? 'active' : ''}>
              {name}
            </a>
          ))}
        </nav>
        <label className="operator">
          值班人員
          <input
            value={operator}
            onChange={(e) => {
              setOp(e.target.value)
              setOperator(e.target.value)
            }}
            aria-label="值班人員姓名（核准紀錄會記下這個名字）"
          />
        </label>
      </header>

      <main>
        {online === false && (
          <div className="banner">
            <b>連不到後端（{API_BASE}）。</b>
            請在另一個終端機啟動：<code>python -m uvicorn backend.main:app --reload --reload-dir backend --reload-dir aiot</code>
            ，看到「API 已就緒」後這裡會自動連上。
          </div>
        )}
        {online === true &&
          (route.page === 'overview' ? (
            <Overview onOpen={(id) => go(`/site/${id}`)} />
          ) : (
            <SitePage
              key={route.id}
              siteId={route.id}
              initialAt={route.at}
              onAtChange={(at) => {
                const hash = `#/site/${route.id}?at=${at}`
                if (window.location.hash !== hash) window.history.replaceState(null, '', hash)
              }}
            />
          ))}
        {online === null && <p className="empty">連線中…</p>}
      </main>
    </div>
  )
}
