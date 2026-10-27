import { useState } from 'react'
import type { FormEvent } from 'react'
import { authApi } from '../api'
import type { User } from '../api'

export function AccountPage({ user }: { user: User }) {
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirm, setConfirm] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (newPw.length < 8) return setMsg({ ok: false, text: '新密碼至少 8 個字元' })
    if (newPw !== confirm) return setMsg({ ok: false, text: '兩次輸入的新密碼不一樣' })
    setBusy(true)
    try {
      await authApi.changePassword(oldPw, newPw)
      setMsg({ ok: true, text: '密碼已修改，下次登入請用新密碼。' })
      setOldPw('')
      setNewPw('')
      setConfirm('')
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account">
      <h1>我的帳號</h1>
      <p className="sub">
        {user.display_name}（{user.username}），角色：{user.role_name}
      </p>
      <form className="panel form" onSubmit={submit} noValidate>
        <h2>修改密碼</h2>
        <label>
          目前的密碼
          <input type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} autoComplete="current-password" />
        </label>
        <label>
          新密碼（至少 8 個字元）
          <input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} autoComplete="new-password" />
        </label>
        <label>
          再輸入一次新密碼
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
        </label>
        {msg && <p className={msg.ok ? 'success' : 'error'}>{msg.text}</p>}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? '儲存中' : '修改密碼'}
        </button>
      </form>
    </div>
  )
}
