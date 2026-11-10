"""AIoT 決策閉環 API（FastAPI）

本機開發：python -m uvicorn backend.main:app --reload --reload-dir backend --reload-dir aiot
API 文件：http://127.0.0.1:8000/docs
部署：     同一個服務也提供打包好的前端網頁（frontend/dist），見 Dockerfile
"""
import copy
import os
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from aiot.config import SITES
from . import auth
from . import control
from .control import SIM
from .engine import Engine
from .store import Store, WO_STATUS

state = {"status": "loading", "error": None}
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _load():
    try:
        from .warm import load_or_build
        state["engine"] = load_or_build()
        state["status"] = "ok"
        print("API 已就緒：http://127.0.0.1:8000/docs", flush=True)
    except Exception as e:  # 讓錯誤顯示在 /api/health，而不是整個服務掛掉
        state["status"], state["error"] = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 先讓伺服器開始接受連線，再在背景載入資料與模型；
    # 雲端平台會檢查埠號是否開啟，載入期間 /api/health 回報 loading。
    state["store"] = Store()
    auth.bootstrap(state["store"], Path(__file__).resolve().parent.parent / "outputs")
    print("載入資料與模型中（本機約 1 分鐘）…", flush=True)
    threading.Thread(target=_load, daemon=True).start()
    yield


app = FastAPI(
    title="AIoT 決策閉環 API",
    description="IoT 感測 → 大數據 → AI 分析 → 決策引擎 → 執行回饋。"
                "所有查詢都可用 `at` 指定「當下時間」，只會使用該時間點以前的資料。",
    version="0.1.0",
    lifespan=lifespan,
)
# 允許前端開發伺服器（Vite 預設 5173 埠）呼叫；部署時前後端同網域，不需要跨網域設定。
# 其他網域可用環境變數 ALLOWED_ORIGINS（逗號分隔）加入。
_origins = ["http://localhost:5173", "http://127.0.0.1:5173"] + [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])

AT_DOC = "當下時間，例如 2026-08-10T14:00；不填 = 該場域最後一筆資料"


def _ready():
    if state["status"] != "ok":
        raise HTTPException(503, "系統啟動中，正在載入資料與模型" if state["status"] == "loading"
                            else f"系統啟動失敗：{state['error']}")


def eng() -> Engine:
    _ready()
    return state["engine"]


def db() -> Store:
    return state["store"]


# ---------------------------------------------------------------- 登入與權限
_bearer = HTTPBearer(auto_error=False)


def current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    try:
        return auth.user_from_token(db(), cred.credentials if cred else None)
    except auth.AuthError as e:
        raise HTTPException(e.code, e.detail, headers={"WWW-Authenticate": "Bearer"} if e.code == 401 else None)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "只有管理員可以使用這項功能")
    return user


def _auth_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except auth.AuthError as e:
        raise HTTPException(e.code, e.detail)


def check_site(site_id):
    if site_id not in SITES:
        raise HTTPException(404, f"找不到場域 {site_id}，可用：{', '.join(SITES)}")


def with_review_status(snap):
    for d in snap["decisions"]:
        rv = db().latest_review(d["id"])
        d["審核"] = None if rv is None else {"action": rv["action"], "operator": rv["operator"], "at": rv["created_at"]}
        d["控制指令"] = control.plan(d["site_id"], d)
    for w in snap["work_orders"]:
        w["狀態"] = db().wo_status(w["id"])
    return snap


# ================================================================ 基本
if not DIST.is_dir():
    # 沒有打包好的前端（本機開發時）：首頁導向 API 文件
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")


@app.get("/api/health", tags=["系統"], summary="系統狀態：ok／loading（啟動中）／error")
def health():
    return {"status": state["status"], "error": state["error"], "sites": len(SITES)}


# ================================================================ 場域（現在發生什麼／接下來會怎樣）
@app.get("/api/sites", tags=["場域"], summary="所有場域的狀態總覽")
def list_sites(at: Optional[str] = Query(None, description=AT_DOC + "（超出某場域資料範圍時，該場域自動用最接近的時間）"), user: dict = Depends(current_user)):
    out = []
    for sid in SITES:
        s = eng().snapshot(sid, at)
        st = s["status"]
        out.append({"site_id": sid, "name": s["name"], "domain": s["domain"], "at": s["at"], "range": s["range"],
                    "指數": st["指數"], "數值": st["數值"], "等級": st["等級"], "等級碼": st["等級碼"],
                    "感測可信度": st["感測可信度"], "決策數": len(s["decisions"]), "工單數": len(s["work_orders"]),
                    "待人工確認": sum(1 for d in s["decisions"] if d["執行模式"] == "人工確認")})
    return out


@app.get("/api/sites/{site_id}", tags=["場域"], summary="單一場域的完整快照（狀態、診斷、預測、決策、工單）")
def site_snapshot(site_id: str, at: Optional[str] = Query(None, description=AT_DOC), user: dict = Depends(current_user)):
    check_site(site_id)
    snap = with_review_status(copy.deepcopy(eng().snapshot(site_id, at)))
    auto_dispatch(snap)
    return snap


SYSTEM_USER = {"username": "system", "display_name": "系統（自動執行）", "role": "system"}


def auto_dispatch(snap):
    """「自動執行」且需要動設備的決策：系統直接下發。設備狀態沒變（與上一筆指令相同）就不重複下發。"""
    at = str(snap.get("at", ""))[:16]
    for d in snap["decisions"]:
        p = d["控制指令"]
        if d["執行模式"] != "自動執行" or p["類型"] != "設備指令":
            continue
        last = db().last_device_command(d["site_id"], d["設備"])
        if last and last["plan"].get("寫入") == p.get("寫入") and last["status"] != "下發失敗":
            continue
        c = SIM.send(db(), d["site_id"], d["id"], at, d, p, SYSTEM_USER["display_name"], auto=True)
        db().log(SYSTEM_USER, "command", d["site_id"], f"{d['設備']}：{d['動作']}", _cmd_detail(c, at))


def _cmd_detail(c, at):
    return {"依據資料時間": at, "指令": c["plan"].get("說明"), "狀態": c["status"], "command_id": c["id"],
            "寫入": "；".join(f"{w['點位']} {w['位址']}＝{w['值']}" for w in c["plan"].get("寫入", []))}


@app.get("/api/sites/{site_id}/series", tags=["場域"], summary="時間序列（畫即時曲線用）")
def site_series(site_id: str,
                vars: Optional[str] = Query(None, description="逗號分隔的欄位，例如 thi,temp_avg；不填 = 關鍵欄位"),
                hours: int = Query(72, ge=1, le=24 * 200, description="往前取幾小時"),
                step: int = Query(1, ge=1, le=24 * 7, description="彙整刻度（小時）；>1 時回傳每格的平均、最小、最大值"),
                at: Optional[str] = Query(None, description=AT_DOC),
                user: dict = Depends(current_user)):
    check_site(site_id)
    cols = vars.split(",") if vars else SITES[site_id]["key"]
    return eng().series(site_id, cols, at, hours, step)


@app.get("/api/sites/{site_id}/timeline", tags=["場域"], summary="整段期間的主指標、每小時等級與事件標記（時間拉條用）")
def site_timeline(site_id: str, user: dict = Depends(current_user)):
    check_site(site_id)
    return eng().timeline(site_id)


@app.get("/api/sites/{site_id}/columns", tags=["場域"], summary="可查詢的欄位")
def site_columns(site_id: str, user: dict = Depends(current_user)):
    check_site(site_id)
    return eng().columns(site_id)


# ================================================================ 決策（現在該做什麼）
@app.get("/api/decisions", tags=["決策"], summary="所有場域的標準決策資料")
def list_decisions(at: Optional[str] = Query(None, description=AT_DOC),
                   mode: Optional[str] = Query(None, description="篩選執行模式：自動執行／人工確認／Fallback"),
                   user: dict = Depends(current_user)):
    out = []
    for sid in SITES:
        snap = with_review_status(copy.deepcopy(eng().snapshot(sid, at)))
        out += [d for d in snap["decisions"] if mode is None or d["執行模式"] == mode]
    return sorted(out, key=lambda d: d["優先"])


class Review(BaseModel):
    action: str = Field(..., description="approve（核准）或 reject（駁回）", examples=["approve"])
    note: Optional[str] = Field(None, description="備註或駁回原因")


@app.post("/api/decisions/{decision_id}/review", tags=["決策"], summary="人工核准或駁回一項決策（操作人＝登入者）")
def review_decision(decision_id: str, body: Review, user: dict = Depends(current_user)):
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action 只能是 approve 或 reject")
    rec = eng().decision_index.get(decision_id)
    if rec is None:
        raise HTTPException(404, "找不到這項決策；請先查詢該場域快照取得最新的決策 id")
    at = rec["at"].strftime("%Y-%m-%dT%H:%M")
    note = (body.note or "").strip()[:200] or None
    rid = db().add_review(decision_id, rec["site_id"], at, body.action, user["display_name"], note, rec["decision"])
    d = rec["decision"]
    db().log(user, body.action, rec["site_id"], f"{d['設備']}：{d['動作']}",
             {"依據資料時間": at, "執行模式": d["執行模式"], "風險": d["風險"], "備註": note, "review_id": rid})
    # 05 執行：核准且需要動設備 → 產生控制指令送往（模擬）控制器
    cmd = None
    p = control.plan(rec["site_id"], d)
    if body.action == "approve" and p["類型"] == "設備指令":
        cmd = SIM.send(db(), rec["site_id"], decision_id, at, d, p, user["display_name"])
        db().log(user, "command", rec["site_id"], f"{d['設備']}：{d['動作']}", _cmd_detail(cmd, at))
    return {"review_id": rid, "decision_id": decision_id, "action": body.action, "site_id": rec["site_id"], "at": at,
            "控制計畫": p, "command": cmd}


# ================================================================ 05 執行（模擬控制器）
@app.get("/api/control/commands", tags=["執行"], summary="控制指令紀錄（含模擬控制器回報）")
def list_commands(site_id: Optional[str] = None, limit: int = Query(50, ge=1, le=500),
                  user: dict = Depends(current_user)):
    SIM.refresh(db())
    return {"offline": SIM.is_offline(site_id) if site_id else sorted(SIM.offline),
            "commands": db().commands(site_id, limit)}


class CommandUpdate(BaseModel):
    note: Optional[str] = Field(None, description="現場處理說明")


@app.post("/api/control/commands/{cid}/manual-done", tags=["執行"], summary="下發失敗改派人工後，人員回報已在現場完成")
def command_manual_done(cid: int, body: CommandUpdate, user: dict = Depends(current_user)):
    c = db().command(cid)
    if c is None:
        raise HTTPException(404, "找不到這筆指令")
    if c["status"] != "下發失敗":
        raise HTTPException(409, "只有「下發失敗」的指令需要人工回報")
    note = (body.note or "").strip()[:200]
    c = db().update_command(cid, "人工已完成", user["display_name"],
                            f"{c['note']}；{user['display_name']} 回報現場已完成" + (f"：{note}" if note else ""))
    db().log(user, "command_manual", c["site_id"], f"{c['device']}：{c['action']}",
             {"依據資料時間": c["data_time"], "指令": c["plan"].get("說明"), "狀態": "人工已完成", "備註": note or None,
              "command_id": cid})
    return c


class SimBody(BaseModel):
    site_id: str
    offline: bool = Field(..., description="true = 模擬控制器通訊中斷")


@app.post("/api/control/sim", tags=["執行"], summary="模擬通訊中斷（測試下發失敗 → 改派人工的流程）")
def control_sim(body: SimBody, user: dict = Depends(current_user)):
    check_site(body.site_id)
    SIM.set_offline(body.site_id, body.offline)
    db().log(user, "sim", body.site_id, "模擬控制器", {"通訊": "中斷" if body.offline else "恢復"})
    return {"site_id": body.site_id, "offline": SIM.is_offline(body.site_id)}


@app.get("/api/reviews", tags=["決策"], summary="審核紀錄（執行回饋的資料來源）")
def list_reviews(site_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000),
                 user: dict = Depends(current_user)):
    return db().reviews(site_id, limit)


# ================================================================ 工單（管理優先）
@app.get("/api/work-orders", tags=["工單"], summary="所有工單（依優先分數排序）")
def list_work_orders(at: Optional[str] = Query(None, description=AT_DOC), user: dict = Depends(current_user)):
    out = []
    for sid in SITES:
        snap = with_review_status(copy.deepcopy(eng().snapshot(sid, at)))
        out += snap["work_orders"]
    return sorted(out, key=lambda w: -w["優先分數"])


class WOUpdate(BaseModel):
    status: str = Field(..., description="、".join(WO_STATUS), examples=["處理中"])


@app.patch("/api/work-orders/{wo_id}", tags=["工單"], summary="更新工單狀態（操作人＝登入者）")
def update_work_order(wo_id: str, body: WOUpdate, user: dict = Depends(current_user)):
    if body.status not in WO_STATUS:
        raise HTTPException(422, f"status 只能是：{'、'.join(WO_STATUS)}")
    for sid in SITES:
        for w in eng().snapshot(sid)["work_orders"]:
            if w["id"] == wo_id:
                before = db().wo_status(wo_id)["status"]
                res = db().set_wo_status(wo_id, sid, w["工單"], body.status, user["display_name"])
                if before != body.status:
                    db().log(user, "work_order", sid, w["工單"], {"從": before, "改為": body.status})
                return {"wo_id": wo_id, **res}
    raise HTTPException(404, "找不到這張工單")


# ================================================================ 執行回饋
@app.get("/api/feedback", tags=["執行回饋"], summary="動作成效驗證結果")
def feedback(user: dict = Depends(current_user)):
    from .engine import jsonable
    e = eng()
    return jsonable({"sheep_effects": e.verify["effects"], "motor_effect": e.motor_effect,
                     "controller_abnormal_hours": e.verify["abnormal_hours"]})


@app.get("/api/feedback/outcomes", tags=["執行回饋"], summary="每筆核准的成效追蹤（依當下時間評估是否達成目標）")
def feedback_outcomes(site_id: Optional[str] = None,
                      at: Optional[str] = Query(None, description=AT_DOC + "；不填 = 各場域最後一筆資料（最終結果）"),
                      limit: int = Query(200, ge=1, le=1000),
                      user: dict = Depends(current_user)):
    from .engine import jsonable
    from .loop import evaluate
    if site_id:
        check_site(site_id)
    out = []
    for r in db().reviews(site_id, limit):
        now = eng().resolve_time(r["site_id"], at)
        if pd.Timestamp(r["at"]) > now:
            continue  # 回放到核准之前的時間點：這筆核准「還沒發生」
        out.append(evaluate(eng(), r, now))
    return jsonable(out)


@app.get("/api/feedback/rules", tags=["執行回饋"], summary="規則學習：每條規則的核准／駁回統計與成效驗證進度")
def feedback_rules(user: dict = Depends(current_user)):
    from .engine import jsonable
    from .loop import rule_stats
    return jsonable(rule_stats(eng(), db().reviews(None, 10000)))


# ================================================================ 登入與帳號
class LoginBody(BaseModel):
    username: str = Field(..., examples=["admin"])
    password: str = Field(..., examples=["你的密碼"])


@app.post("/api/auth/login", tags=["帳號"], summary="登入，取得 token（之後請求放在 Authorization: Bearer 標頭）")
def login(body: LoginBody):
    return _auth_call(auth.login, db(), body.username.strip(), body.password)


@app.get("/api/auth/me", tags=["帳號"], summary="目前登入者")
def me(user: dict = Depends(current_user)):
    return user


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/password", tags=["帳號"], summary="修改自己的密碼")
def change_password(body: PasswordBody, user: dict = Depends(current_user)):
    u = db().get_user(user["username"])
    if not auth.check_password(body.old_password, u["password_hash"]):
        raise HTTPException(400, "目前的密碼不正確")
    _auth_call(auth.validate_password, body.new_password)
    db().update_user(user["username"], password_hash=auth.hash_password(body.new_password))
    db().log(user, "password_change", target=user["username"])
    return {"ok": True}


# ================================================================ 管理員
@app.get("/api/admin/audit", tags=["管理員"], summary="操作紀錄：誰在什麼時候做了什麼")
def admin_audit(username: Optional[str] = None,
                action: Optional[str] = Query(None, description="login／login_failed／approve／reject／work_order／user_create／user_update／password_change"),
                limit: int = Query(300, ge=1, le=2000),
                user: dict = Depends(admin_user)):
    return db().audit(username, action, limit)


@app.get("/api/admin/users", tags=["管理員"], summary="帳號列表（含各人的操作次數）")
def admin_users(user: dict = Depends(admin_user)):
    users = db().list_users()
    counts = {}
    for a in db().audit(None, None, 100000):
        if a["action"] in ("approve", "reject", "work_order"):
            counts.setdefault(a["username"], {"approve": 0, "reject": 0, "work_order": 0})[a["action"]] += 1
    for u in users:
        u["role_name"] = auth.ROLES.get(u["role"], u["role"])
        u["active"] = bool(u["active"])
        u["counts"] = counts.get(u["username"], {"approve": 0, "reject": 0, "work_order": 0})
    return users


class NewUser(BaseModel):
    username: str
    display_name: str
    password: str
    role: str = Field("staff", description="staff 或 admin")


@app.post("/api/admin/users", tags=["管理員"], summary="新增帳號")
def admin_create_user(body: NewUser, user: dict = Depends(admin_user)):
    _auth_call(auth.validate_username, body.username)
    _auth_call(auth.validate_password, body.password)
    if body.role not in auth.ROLES:
        raise HTTPException(422, "角色只能是 staff 或 admin")
    if not body.display_name.strip():
        raise HTTPException(422, "請填顯示名稱")
    if db().get_user(body.username):
        raise HTTPException(409, "這個帳號已經存在")
    db().create_user(body.username, body.display_name.strip(), body.role, auth.hash_password(body.password))
    db().log(user, "user_create", target=body.username, detail={"顯示名稱": body.display_name.strip(), "角色": auth.ROLES[body.role]})
    return {"ok": True}


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(None, description="重設密碼")


@app.patch("/api/admin/users/{username}", tags=["管理員"], summary="修改帳號：顯示名稱、角色、停用／啟用、重設密碼")
def admin_update_user(username: str, body: UserUpdate, user: dict = Depends(admin_user)):
    target = db().get_user(username)
    if not target:
        raise HTTPException(404, "找不到這個帳號")
    if body.role is not None and body.role not in auth.ROLES:
        raise HTTPException(422, "角色只能是 staff 或 admin")
    losing_admin = target["role"] == "admin" and target["active"] and (
        (body.role is not None and body.role != "admin") or body.active is False)
    if losing_admin and db().count_admins() <= 1:
        raise HTTPException(400, "至少要保留一個啟用中的管理員")
    if username == user["username"] and body.active is False:
        raise HTTPException(400, "不能停用自己的帳號")
    fields, changes = {}, {}
    if body.display_name is not None and body.display_name.strip():
        fields["display_name"] = changes["顯示名稱"] = body.display_name.strip()
    if body.role is not None:
        fields["role"] = body.role
        changes["角色"] = auth.ROLES[body.role]
    if body.active is not None:
        fields["active"] = int(body.active)
        changes["狀態"] = "啟用" if body.active else "停用"
    if body.password:
        _auth_call(auth.validate_password, body.password)
        fields["password_hash"] = auth.hash_password(body.password)
        changes["密碼"] = "已重設"
    db().update_user(username, **fields)
    db().log(user, "user_update", target=username, detail=changes)
    return {"ok": True}


# ================================================================ 前端網頁（部署時）
# 放在所有 API 之後：/api/... 與 /docs 先比對，其餘路徑交給打包好的前端
if DIST.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="web")
