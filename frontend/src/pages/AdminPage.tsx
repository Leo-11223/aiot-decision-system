import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { adminApi } from '../api'
import type { AdminUser, AuditEntry, User } from '../api'

// 管理員：操作紀錄（誰在什麼時候做了什麼）＋帳號管理

const ACTIONS: Record<string, string> = {
  approve: '核准決策',
  reject: '駁回決策',
  work_order: '更新工單',
  login: '登入',
  login_failed: '登入失敗',
  password_change: '修改密碼',
  user_create: '新增帳號',
  user_update: '修改帳號',
}

const fmtTs = (ts: string) => ts.replace('T', ' ').slice(5, 16)

function detailText(a: AuditEntry): string {
  const d = a.detail
  if (a.action === 'approve' || a.action === 'reject') {
    // 「依據資料時間」是這筆決策根據的感測資料時間點（時間拉條所在位置），不是按下按鈕的時間
    const raw = d['依據資料時間'] ?? d['決策時間點']
    const at = typeof raw === 'string' ? raw.replace('T', ' ').replace(/-/g, '/').slice(0, 16) : ''
    return [at && `依據資料時間 ${at}`, d['執行模式'], d['備註'] ? `原因：${d['備註']}` : null].filter(Boolean).join('，')
  }
  if (a.action === 'work_order') return `${d['從']} → ${d['改為']}`
  if (a.action === 'login_failed') return `連續第 ${d['次數']} 次`
  return Object.entries(d)
    .map(([k, v]) => `${k}：${v}`)
    .join('，')
}

export function AdminPage({ me, siteName }: { me: User; siteName: (id: string) => string }) {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [audit, setAudit] = useState<AuditEntry[] | null>(null)
  const [who, setWho] = useState('')
  const [action, setAction] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [u, a] = await Promise.all([adminApi.users(), adminApi.audit(who || undefined, action || undefined)])
      setUsers(u)
      setAudit(a)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [who, action])

  useEffect(() => {
    reload()
  }, [reload])

  return (
    <div className="admin">
      <div className="overview-head">
        <h1>管理</h1>
        <p className="sub">查看每位人員的操作，並管理帳號。紀錄依操作時間由新到舊排列（台灣時間）；「依據資料時間」是該筆決策所根據的感測資料時間點。</p>
      </div>
      {error && <p className="error">{error}</p>}

      <section className="panel">
        <div className="panel-head">
          <h2>操作紀錄</h2>
          <div className="controls">
            <label>
              人員
              <select value={who} onChange={(e) => setWho(e.target.value)}>
                <option value="">全部</option>
                {users.map((u) => (
                  <option key={u.username} value={u.username}>
                    {u.display_name}（{u.username}）
                  </option>
                ))}
              </select>
            </label>
            <label>
              動作
              <select value={action} onChange={(e) => setAction(e.target.value)}>
                <option value="">全部</option>
                {Object.entries(ACTIONS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <button onClick={reload}>重新整理</button>
          </div>
        </div>
        {audit && audit.length === 0 && <p className="empty">沒有符合條件的紀錄。</p>}
        {audit && audit.length > 0 && (
          <div className="table-wrap">
            <table className="audit">
              <thead>
                <tr>
                  <th>操作時間</th>
                  <th>人員</th>
                  <th>動作</th>
                  <th>對象</th>
                  <th>內容</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((a) => (
                  <tr key={a.id} className={'act-' + a.action}>
                    <td className="nowrap">{fmtTs(a.ts)}</td>
                    <td className="nowrap">
                      {a.display_name ?? a.username ?? '—'}
                      {a.role === 'admin' && <span className="role-tag">管理員</span>}
                    </td>
                    <td className="nowrap">
                      <span className="act">{ACTIONS[a.action] ?? a.action}</span>
                    </td>
                    <td>
                      {a.site_id ? `${siteName(a.site_id)}：` : ''}
                      {a.target ?? ''}
                    </td>
                    <td className="muted">{detailText(a)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>帳號</h2>
        <div className="table-wrap">
          <table className="audit">
            <thead>
              <tr>
                <th>帳號</th>
                <th>名稱</th>
                <th>角色</th>
                <th>核准／駁回／工單</th>
                <th>最後登入</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <UserRow key={u.username} u={u} isMe={u.username === me.username} onChanged={reload} onError={setError} />
              ))}
            </tbody>
          </table>
        </div>
        <NewUserForm onCreated={reload} />
      </section>
    </div>
  )
}

function UserRow({ u, isMe, onChanged, onError }: { u: AdminUser; isMe: boolean; onChanged: () => void; onError: (m: string) => void }) {
  const [resetting, setResetting] = useState(false)
  const [pw, setPw] = useState('')

  const update = async (body: { role?: string; active?: boolean; password?: string }) => {
    try {
      await adminApi.updateUser(u.username, body)
      setResetting(false)
      setPw('')
      onChanged()
    } catch (e) {
      onError((e as Error).message)
    }
  }

  return (
    <tr className={u.active ? '' : 'inactive'}>
      <td className="nowrap">{u.username}</td>
      <td className="nowrap">
        {u.display_name}
        {isMe && <span className="role-tag">你</span>}
      </td>
      <td className="nowrap">
        <select value={u.role} onChange={(e) => update({ role: e.target.value })} aria-label={`${u.display_name} 的角色`}>
          <option value="staff">員工</option>
          <option value="admin">管理員</option>
        </select>
      </td>
      <td className="nowrap">
        {u.counts.approve}／{u.counts.reject}／{u.counts.work_order}
      </td>
      <td className="nowrap muted">{u.last_login ? fmtTs(u.last_login) : '未登入過'}</td>
      <td>
        <div className="row-actions">
          {resetting ? (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                if (pw.length < 8) return onError('新密碼至少 8 個字元')
                update({ password: pw })
              }}
            >
              <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="新密碼" aria-label="新密碼" autoFocus />
              <button type="submit">確認</button>
              <button type="button" onClick={() => setResetting(false)}>
                取消
              </button>
            </form>
          ) : (
            <>
              <button onClick={() => setResetting(true)}>重設密碼</button>
              {!isMe && <button onClick={() => update({ active: !u.active })}>{u.active ? '停用' : '啟用'}</button>}
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function NewUserForm({ onCreated }: { onCreated: () => void }) {
  const [f, setF] = useState({ username: '', display_name: '', password: '', role: 'staff' })
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!f.username || !f.display_name || !f.password) return setMsg({ ok: false, text: '請填完帳號、名稱和密碼' })
    if (f.password.length < 8) return setMsg({ ok: false, text: '密碼至少 8 個字元' })
    try {
      await adminApi.createUser(f)
      setMsg({ ok: true, text: `已新增 ${f.display_name}（${f.username}）` })
      setF({ username: '', display_name: '', password: '', role: 'staff' })
      onCreated()
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message })
    }
  }

  return (
    <form className="new-user" onSubmit={submit} noValidate>
      <h3>新增帳號</h3>
      <div className="new-user-fields">
        <label>
          帳號（英數字）
          <input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value.trim() })} autoComplete="off" />
        </label>
        <label>
          顯示名稱
          <input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} />
        </label>
        <label>
          初始密碼
          <input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" />
        </label>
        <label>
          角色
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            <option value="staff">員工</option>
            <option value="admin">管理員</option>
          </select>
        </label>
        <button className="primary" type="submit">
          新增帳號
        </button>
      </div>
      {msg && <p className={msg.ok ? 'success' : 'error'}>{msg.text}</p>}
    </form>
  )
}
