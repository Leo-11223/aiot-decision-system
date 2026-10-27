import { useState } from 'react'
import type { FormEvent } from 'react'
import { authApi, setToken } from '../api'
import type { User } from '../api'

type Props = { onLogin: (u: User) => void; notice?: string | null }

export function LoginPage({ onLogin, notice }: Props) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('請輸入帳號和密碼')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await authApi.login(username.trim(), password)
      setToken(res.token)
      onLogin(res.user)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit} noValidate>
        <h1>AIoT 決策閉環</h1>
        <p className="sub">登入後才能查看場域資料與處理決策。</p>
        {notice && <p className="notice">{notice}</p>}
        <label>
          帳號
          <input
            value={username}
            onChange={(e) => {
              setUsername(e.target.value)
              setError(null)
            }}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label>
          密碼
          <input
            type="password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value)
              setError(null)
            }}
            autoComplete="current-password"
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? '登入中' : '登入'}
        </button>
        <p className="hint">沒有帳號請找管理員建立。</p>
      </form>
    </div>
  )
}
