"""SQLite 紀錄：人工核准／駁回決策、工單狀態。這就是閉環中「人工確認」與「派工」的落地紀錄。"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

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
            """)
            self.conn.commit()

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

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
