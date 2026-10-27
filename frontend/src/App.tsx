import { useEffect, useState } from 'react'
import { API_BASE, UNAUTHORIZED_EVENT, api, authApi, getToken, setToken } from './api'
import type { User } from './api'
import { AccountPage } from './pages/AccountPage'
import { AdminPage } from './pages/AdminPage'
import { FeedbackPage } from './pages/FeedbackPage'
import { LoginPage } from './pages/LoginPage'
import { Overview } from './pages/Overview'
import { SitePage } from './pages/SitePage'

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

type Route =
  | { page: 'overview' }
  | { page: 'feedback' }
  | { page: 'admin' }
  | { page: 'account' }
  | { page: 'site'; id: string; at?: string }

function parseHash(): Route {
  const h = window.location.hash.replace(/^#/, '')
  const m = h.match(/^\/site\/([a-z0-9_]+)(?:\?at=([0-9T:-]+))?/)
  if (m && m[1] in SHORT) return { page: 'site', id: m[1], at: m[2] }
  if (h.startsWith('/feedback')) return { page: 'feedback' }
  if (h.startsWith('/admin')) return { page: 'admin' }
  if (h.startsWith('/account')) return { page: 'account' }
  return { page: 'overview' }
}

const siteName = (id: string) => SHORT[id] ?? id

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash())
  const [online, setOnline] = useState<boolean | null>(null)
  const [backend, setBackend] = useState<{ status: string; error: string | null } | null>(null)
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState(true)
  const [loginNotice, setLoginNotice] = useState<string | null>(null)

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // 已存的登入憑證：向後端確認還有效
  useEffect(() => {
    if (!getToken()) {
      setChecking(false)
      return
    }
    authApi
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setChecking(false))
  }, [])

  // 任何請求回 401（過期、帳號被停用）→ 回登入頁
  useEffect(() => {
    const onUnauthorized = () => {
      setUser((u) => {
        if (u) setLoginNotice('登入已過期或帳號已停用，請重新登入。')
        return null
      })
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  // 後端啟動要一段時間：載入中每 3 秒、連不上每 4 秒重試
  useEffect(() => {
    let alive = true
    let timer = 0
    const check = () =>
      api
        .health()
        .then((h) => {
          if (!alive) return
          setBackend(h)
          if (h.status === 'ok') setOnline(true)
          else {
            setOnline(null)
            if (h.status === 'loading') timer = window.setTimeout(check, 3000)
          }
        })
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

  const logout = () => {
    setToken(null)
    setUser(null)
    setLoginNotice(null)
    window.location.hash = '/'
  }

  const banners = (
    <>
      {backend?.status === 'loading' && (
        <div className="banner">
          <b>系統啟動中。</b>正在載入感測資料與預先訓練的模型，完成後會自動進入。若主機剛從休眠喚醒，約需 1 至 2 分鐘。
        </div>
      )}
      {backend?.status === 'error' && (
        <div className="banner">
          <b>系統啟動失敗：</b>
          {backend.error}
        </div>
      )}
      {online === false && (
        <div className="banner">
          <b>連不到後端（{API_BASE || window.location.origin}）。</b>
          本機開發時請在另一個終端機啟動：<code>python -m uvicorn backend.main:app --reload --reload-dir backend --reload-dir aiot</code>
          ，啟動後這裡會自動連上。
        </div>
      )}
    </>
  )

  if (checking) return <p className="empty center">檢查登入狀態…</p>

  if (!user)
    return (
      <div className="app">
        <main>
          {banners}
          <LoginPage
            notice={loginNotice}
            onLogin={(u) => {
              setUser(u)
              setLoginNotice(null)
            }}
          />
        </main>
      </div>
    )

  const isAdmin = user.role === 'admin'
  const page = route.page === 'admin' && !isAdmin ? 'overview' : route.page

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          AIoT 決策閉環
        </a>
        <nav aria-label="頁面">
          <a href="#/" className={page === 'overview' ? 'active' : ''}>
            總覽
          </a>
          {Object.entries(SHORT).map(([id, name]) => (
            <a key={id} href={`#/site/${id}`} className={route.page === 'site' && route.id === id ? 'active' : ''}>
              {name}
            </a>
          ))}
          <a href="#/feedback" className={page === 'feedback' ? 'active' : ''}>
            執行回饋
          </a>
          {isAdmin && (
            <a href="#/admin" className={page === 'admin' ? 'active' : ''}>
              管理
            </a>
          )}
        </nav>
        <div className="whoami">
          <a href="#/account" className={page === 'account' ? 'active' : ''} title="我的帳號">
            {user.display_name}
            <span className={'role-tag' + (isAdmin ? ' admin' : '')}>{user.role_name}</span>
          </a>
          <button onClick={logout}>登出</button>
        </div>
      </header>

      <main>
        {banners}
        {page === 'account' && <AccountPage user={user} />}
        {online === true &&
          (page === 'overview' ? (
            <Overview onOpen={(id) => (window.location.hash = `/site/${id}`)} />
          ) : page === 'feedback' ? (
            <FeedbackPage siteName={siteName} />
          ) : page === 'admin' ? (
            <AdminPage me={user} siteName={siteName} />
          ) : route.page === 'site' ? (
            <SitePage
              key={route.id}
              siteId={route.id}
              initialAt={route.at}
              onAtChange={(at) => {
                const hash = `#/site/${route.id}?at=${at}`
                if (window.location.hash !== hash) window.history.replaceState(null, '', hash)
              }}
            />
          ) : null)}
        {online === null && !backend && <p className="empty">連線中…</p>}
      </main>
    </div>
  )
}
