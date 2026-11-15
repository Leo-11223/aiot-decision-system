"""SQLite 紀錄：人工核准／駁回決策、控制指令、工單狀態、帳號、操作紀錄。
這就是閉環中「人工確認」與「派工」的落地紀錄，也是管理員稽核的依據。"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))  # 紀錄一律用台灣時間（雲端主機預設是 UTC）

DB_PATH = Path(__file__).resolve().parent.parent / "outputs" / "aiot.db"
WO_STATUS = ["待派工", "處理中", "已完成"]


class Store:
    def __init__(self, path=DB_PATH):
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL, site_id TEXT NOT NULL, at TEXT NOT NULL,
                action TEXT NOT NULL, operator TEXT, note TEXT,
                decision_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_orders (
                wo_id TEXT PRIMARY KEY, site_id TEXT, title TEXT, status TEXT NOT NULL,
                operator TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
                password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_login TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                username TEXT, display_name TEXT, role TEXT,
                action TEXT NOT NULL, site_id TEXT, target TEXT, detail_json TEXT);
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, decision_id TEXT,
                data_time TEXT, device TEXT, action TEXT, plan_json TEXT NOT NULL,
                status TEXT NOT NULL, operator TEXT, auto INTEGER NOT NULL DEFAULT 0, note TEXT,
                sent_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_by TEXT);
            """)
            self.conn.commit()

    @staticmethod
    def _now():
        return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")

    # ------------------------------------------------------------ 決策審核
    def add_review(self, decision_id, site_id, at, action, operator, note, decision):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO reviews(decision_id, site_id, at, action, operator, note, decision_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (decision_id, site_id, at, action, operator, note, json.dumps(decision, ensure_ascii=False), self._now()))
            self.conn.commit()
            return cur.lastrowid

    def latest_review(self, decision_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM reviews WHERE decision_id=? ORDER BY id DESC LIMIT 1",
                                    (decision_id,)).fetchone()
        return self._review(row) if row else None

    def reviews(self, site_id=None, limit=100):
        q, args = "SELECT * FROM reviews", []
        if site_id:
            q, args = q + " WHERE site_id=?", [site_id]
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [self._review(r) for r in rows]

    @staticmethod
    def _review(r):
        d = dict(r)
        d["decision"] = json.loads(d.pop("decision_json"))
        return d

    # ------------------------------------------------------------ 控制指令（05 執行）
    def add_command(self, site_id, decision_id, data_time, device, action, plan, status, operator, note, auto):
        now = self._now()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO commands(site_id, decision_id, data_time, device, action, plan_json, status, operator, auto,"
                " note, sent_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (site_id, decision_id, data_time, device, action, json.dumps(plan, ensure_ascii=False), status,
                 operator, int(auto), note, now, now))
            self.conn.commit()
            cid = cur.lastrowid
        return self.command(cid)

    def command(self, cid):
        with self.lock:
            row = self.conn.execute("SELECT * FROM commands WHERE id=?", (cid,)).fetchone()
        return self._command(row) if row else None

    def commands(self, site_id=None, limit=50):
        q, args = "SELECT * FROM commands", []
        if site_id:
            q, args = q + " WHERE site_id=?", [site_id]
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [self._command(r) for r in rows]

    def pending_commands(self, cutoff):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM commands WHERE status='已下發' AND sent_at<=?", (cutoff,)).fetchall()
        return [self._command(r) for r in rows]

    def last_decision_command(self, decision_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM commands WHERE decision_id=? ORDER BY id DESC LIMIT 1",
                                    (decision_id,)).fetchone()
        return self._command(row) if row else None

    def last_device_command(self, site_id, device):
        with self.lock:
            row = self.conn.execute("SELECT * FROM commands WHERE site_id=? AND device=? ORDER BY id DESC LIMIT 1",
                                    (site_id, device)).fetchone()
        return self._command(row) if row else None

    def update_command(self, cid, status, done_by=None, note=None):
        with self.lock:
            self.conn.execute("UPDATE commands SET status=?, updated_at=?, done_by=COALESCE(?, done_by),"
                              " note=COALESCE(?, note) WHERE id=?", (status, self._now(), done_by, note, cid))
            self.conn.commit()
        return self.command(cid)

    @staticmethod
    def _command(r):
        d = dict(r)
        d["plan"] = json.loads(d.pop("plan_json"))
        d["auto"] = bool(d["auto"])
        return d

    # ------------------------------------------------------------ 工單
    def wo_status(self, wo_id):
        with self.lock:
            row = self.conn.execute("SELECT status, operator, updated_at FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        return dict(row) if row else {"status": WO_STATUS[0], "operator": None, "updated_at": None}

    def set_wo_status(self, wo_id, site_id, title, status, operator):
        with self.lock:
            self.conn.execute(
                "INSERT INTO work_orders(wo_id, site_id, title, status, operator, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(wo_id) DO UPDATE SET status=excluded.status, operator=excluded.operator, updated_at=excluded.updated_at",
                (wo_id, site_id, title, status, operator, self._now()))
            self.conn.commit()
        return self.wo_status(wo_id)

    # ------------------------------------------------------------ 帳號
    def get_user(self, username):
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with self.lock:
            rows = self.conn.execute("SELECT username, display_name, role, active, created_at, last_login "
                                     "FROM users ORDER BY role, username").fetchall()
        return [dict(r) for r in rows]

    def create_user(self, username, display_name, role, password_hash):
        with self.lock:
            self.conn.execute("INSERT INTO users(username, display_name, role, password_hash, active, created_at) "
                              "VALUES (?,?,?,?,1,?)", (username, display_name, role, password_hash, self._now()))
            self.conn.commit()

    def update_user(self, username, **fields):
        allowed = {"display_name", "role", "password_hash", "active", "last_login"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return
        with self.lock:
            self.conn.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE username=?",
                              list(sets.values()) + [username])
            self.conn.commit()

    def count_admins(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]

    # ------------------------------------------------------------ 操作紀錄（稽核）
    def log(self, user, action, site_id=None, target=None, detail=None):
        u = user or {}
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit(ts, username, display_name, role, action, site_id, target, detail_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self._now(), u.get("username"), u.get("display_name"), u.get("role"), action, site_id, target,
                 json.dumps(detail or {}, ensure_ascii=False)))
            self.conn.commit()

    def audit(self, username=None, action=None, limit=300):
        q, args, cond = "SELECT * FROM audit", [], []
        if username:
            cond.append("username=?")
            args.append(username)
        if action:
            cond.append("action=?")
            args.append(action)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            out.append(d)
        return out
