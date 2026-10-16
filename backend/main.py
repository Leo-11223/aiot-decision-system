"""AIoT 決策閉環 API（FastAPI）

啟動：  py -m uvicorn backend.main:app --reload
文件：  http://127.0.0.1:8000/docs
"""
import copy
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from aiot.config import SITES
from .engine import Engine
from .store import Store, WO_STATUS

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("載入資料並訓練模型中（約 1 分鐘）…", flush=True)
    state["engine"] = Engine()
    state["store"] = Store()
    print("API 已就緒：http://127.0.0.1:8000/docs", flush=True)
    yield


app = FastAPI(
    title="AIoT 決策閉環 API",
    description="IoT 感測 → 大數據 → AI 分析 → 決策引擎 → 執行回饋。"
                "所有查詢都可用 `at` 指定「當下時間」，只會使用該時間點以前的資料。",
    version="0.1.0",
    lifespan=lifespan,
)
# 允許前端開發伺服器（Vite 預設 5173 埠）呼叫
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])

AT_DOC = "當下時間，例如 2026-08-10T14:00；不填 = 該場域最後一筆資料"


def eng() -> Engine:
    return state["engine"]


def db() -> Store:
    return state["store"]


def check_site(site_id):
    if site_id not in SITES:
        raise HTTPException(404, f"找不到場域 {site_id}，可用：{', '.join(SITES)}")


def with_review_status(snap):
    for d in snap["decisions"]:
        rv = db().latest_review(d["id"])
        d["審核"] = None if rv is None else {"action": rv["action"], "operator": rv["operator"], "at": rv["created_at"]}
    for w in snap["work_orders"]:
        w["狀態"] = db().wo_status(w["id"])
    return snap


# ================================================================ 基本
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/api/health", tags=["系統"])
def health():
    return {"status": "ok", "sites": len(SITES)}


# ================================================================ 場域（現在發生什麼／接下來會怎樣）
@app.get("/api/sites", tags=["場域"], summary="所有場域的狀態總覽")
def list_sites(at: Optional[str] = Query(None, description=AT_DOC + "（超出某場域資料範圍時，該場域自動用最接近的時間）")):
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
def site_snapshot(site_id: str, at: Optional[str] = Query(None, description=AT_DOC)):
    check_site(site_id)
    return with_review_status(copy.deepcopy(eng().snapshot(site_id, at)))


@app.get("/api/sites/{site_id}/series", tags=["場域"], summary="時間序列（畫即時曲線用）")
def site_series(site_id: str,
                vars: Optional[str] = Query(None, description="逗號分隔的欄位，例如 thi,temp_avg；不填 = 關鍵欄位"),
                hours: int = Query(72, ge=1, le=24 * 200, description="往前取幾小時"),
                step: int = Query(1, ge=1, le=24 * 7, description="彙整刻度（小時）；>1 時回傳每格的平均、最小、最大值"),
                at: Optional[str] = Query(None, description=AT_DOC)):
    check_site(site_id)
    cols = vars.split(",") if vars else SITES[site_id]["key"]
    return eng().series(site_id, cols, at, hours, step)


@app.get("/api/sites/{site_id}/timeline", tags=["場域"], summary="整段期間的主指標、每小時等級與事件標記（時間拉條用）")
def site_timeline(site_id: str):
    check_site(site_id)
    return eng().timeline(site_id)


@app.get("/api/sites/{site_id}/columns", tags=["場域"], summary="可查詢的欄位")
def site_columns(site_id: str):
    check_site(site_id)
    return eng().columns(site_id)


# ================================================================ 決策（現在該做什麼）
@app.get("/api/decisions", tags=["決策"], summary="所有場域的標準決策資料")
def list_decisions(at: Optional[str] = Query(None, description=AT_DOC),
                   mode: Optional[str] = Query(None, description="篩選執行模式：自動執行／人工確認／Fallback")):
    out = []
    for sid in SITES:
        snap = with_review_status(copy.deepcopy(eng().snapshot(sid, at)))
        out += [d for d in snap["decisions"] if mode is None or d["執行模式"] == mode]
    return sorted(out, key=lambda d: d["優先"])


class Review(BaseModel):
    action: str = Field(..., description="approve（核准）或 reject（駁回）", examples=["approve"])
    operator: str = Field("現場人員", description="操作人")
    note: Optional[str] = Field(None, description="備註")


@app.post("/api/decisions/{decision_id}/review", tags=["決策"], summary="人工核准或駁回一項決策")
def review_decision(decision_id: str, body: Review):
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action 只能是 approve 或 reject")
    rec = eng().decision_index.get(decision_id)
    if rec is None:
        raise HTTPException(404, "找不到這項決策；請先查詢該場域快照取得最新的決策 id")
    at = rec["at"].strftime("%Y-%m-%dT%H:%M")
    rid = db().add_review(decision_id, rec["site_id"], at, body.action, body.operator, body.note, rec["decision"])
    return {"review_id": rid, "decision_id": decision_id, "action": body.action, "site_id": rec["site_id"], "at": at}


@app.get("/api/reviews", tags=["決策"], summary="審核紀錄（執行回饋的資料來源）")
def list_reviews(site_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
    return db().reviews(site_id, limit)


# ================================================================ 工單（管理優先）
@app.get("/api/work-orders", tags=["工單"], summary="所有工單（依優先分數排序）")
def list_work_orders(at: Optional[str] = Query(None, description=AT_DOC)):
    out = []
    for sid in SITES:
        snap = with_review_status(copy.deepcopy(eng().snapshot(sid, at)))
        out += snap["work_orders"]
    return sorted(out, key=lambda w: -w["優先分數"])


class WOUpdate(BaseModel):
    status: str = Field(..., description="、".join(WO_STATUS), examples=["處理中"])
    operator: str = Field("現場人員")


@app.patch("/api/work-orders/{wo_id}", tags=["工單"], summary="更新工單狀態")
def update_work_order(wo_id: str, body: WOUpdate):
    if body.status not in WO_STATUS:
        raise HTTPException(422, f"status 只能是：{'、'.join(WO_STATUS)}")
    for sid in SITES:
        for w in eng().snapshot(sid)["work_orders"]:
            if w["id"] == wo_id:
                return {"wo_id": wo_id, **db().set_wo_status(wo_id, sid, w["工單"], body.status, body.operator)}
    raise HTTPException(404, "找不到這張工單")


# ================================================================ 執行回饋
@app.get("/api/feedback", tags=["執行回饋"], summary="動作成效驗證結果")
def feedback():
    from .engine import jsonable
    e = eng()
    return jsonable({"sheep_effects": e.verify["effects"], "motor_effect": e.motor_effect,
                     "controller_abnormal_hours": e.verify["abnormal_hours"]})
