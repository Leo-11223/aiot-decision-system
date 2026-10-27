"""05 執行回饋（閉環）：人工核准之後發生了什麼？

1. 成效追蹤：每類決策有可量化的目標與期限，依「當下時間」檢查是否達成。
2. 核對現場紀錄：比對設備紀錄，確認該期間設備是否真的有動作。
   現場沒有執行 → 不計入成效樣本（誠實區分「環境自己變好」與「動作造成的改善」）。
3. 規則學習：統計每條規則的核准／駁回；駁回率過高 → 建議檢討規則；
   成效樣本數達門檻且統計顯著 → 該動作才可升級為自動執行。
"""
import pandas as pd

from aiot.config import MIN_SAMPLES, REVIEW_FLAG_MIN, Z_ONE_SIDED
from aiot.feedback import FANS


# 決策目標規格：用 (場域, 設備關鍵字) 對應
GOALS = [
    {"site": "sheep", "match": "風扇", "metric": "thi", "op": "<", "value": 79, "hold": 2, "deadline": 6,
     "text": "THI 連續 2 小時低於 79", "effect_key": "風扇開啟"},
    {"site": "sheep", "match": "飲水", "metric": "thi", "op": "<", "value": 84, "hold": 2, "deadline": 6,
     "text": "THI 連續 2 小時低於 84（脫離緊急）", "effect_key": None},
    {"site": "sheep", "match": "遮陽", "metric": "thi", "op": "<", "value": 79, "hold": 2, "deadline": 6,
     "text": "THI 連續 2 小時低於 79", "effect_key": "南遮陽張開"},
    {"site": "expert", "match": "CPM 馬達（打氣", "metric": "do", "op": ">=", "value": 4.5, "hold": 3, "deadline": 24,
     "text": "DO 共識值連續 3 小時 ≥ 4.5 mg/L", "effect_key": "馬達運轉"},
    {"site": "site", "match": "灑水", "metric": "pm10", "op": "<", "value": 126, "hold": 2, "deadline": 6,
     "text": "PM10 連續 2 小時低於 126", "effect_key": None},
]


def goal_for(decision: dict):
    for g in GOALS:
        if decision.get("site_id") == g["site"] and g["match"] in decision.get("設備", ""):
            if "維持" in decision.get("動作", "") or "待命" in decision.get("動作", "") or "巡檢" in decision.get("動作", ""):
                return None  # 「維持現狀」類決策沒有要追蹤的改善目標
            return g
    return None


def _metric(engine, site_id, metric):
    if site_id == "expert" and metric == "do":
        return engine.indicator("expert")
    return engine.sites[site_id]["clean"][metric]


def _executed(engine, site_id, goal, t0, t1):
    """現場設備紀錄：該期間是否真的有執行。None = 這類決策沒有設備紀錄可核對。"""
    raw = engine.sites[site_id]["raw"].loc[t0:t1]
    if raw.empty:
        return None, "期間內沒有資料"
    if goal["effect_key"] == "風扇開啟":
        hours = int((raw[FANS] == 1).any(axis=1).sum())
        return hours > 0, f"期間內風扇實際開啟 {hours} 小時" if hours else "現場紀錄顯示風扇未開啟"
    if goal["effect_key"] == "南遮陽張開":
        hours = int((raw["南遮陽張 25/26"] == 1).sum())
        return hours > 0, f"期間內遮陽實際張開 {hours} 小時" if hours else "現場紀錄顯示遮陽未張開"
    if goal["effect_key"] == "馬達運轉":
        df = engine.sites[site_id]["df"].loc[t0:t1]
        online = df["power"].fillna(0) > 5
        stop = int(((df["current"] < 0.5) & online).sum())
        return stop == 0, "期間內馬達持續運轉" if stop == 0 else f"期間內馬達仍停機 {stop} 小時"
    return None, "這類決策沒有設備紀錄可核對"


def evaluate(engine, review: dict, now: pd.Timestamp):
    """依「當下時間 now」評估一筆核准紀錄的成效。"""
    d = review["decision"]
    sid = review["site_id"]
    t0 = pd.Timestamp(review["at"])
    out = {"review_id": review["id"], "decision_id": review["decision_id"], "site_id": sid,
           "設備": d.get("設備"), "動作": d.get("動作"), "核准時間": review["at"], "審核": review["action"],
           "操作人": review["operator"], "備註": review.get("note")}
    if review["action"] != "approve":
        out.update({"狀態": "已駁回", "說明": review.get("note") or "未填原因"})
        return out
    g = goal_for(dict(d, site_id=sid))
    if g is None:
        out.update({"狀態": "無量化目標", "說明": "以人工回報或工單完成為準"})
        return out
    deadline = t0 + pd.Timedelta(hours=g["deadline"])
    out.update({"目標": g["text"], "期限": deadline.strftime("%Y-%m-%dT%H:%M")})
    if now <= t0:
        out.update({"狀態": "追蹤中", "說明": "剛核准，等待資料"})
        return out
    end = min(now, deadline)
    s = _metric(engine, sid, g["metric"]).loc[t0 + pd.Timedelta(hours=1):end]
    ok = (s < g["value"]) if g["op"] == "<" else (s >= g["value"])
    run, hit = 0, None
    for ts, v in ok.items():
        run = run + 1 if v else 0
        if run >= g["hold"]:
            hit = ts
            break
    executed, exec_text = _executed(engine, sid, g, t0, hit or end)
    out["現場執行"] = executed
    out["現場紀錄"] = exec_text
    out["最新值"] = None if s.dropna().empty else round(float(s.dropna().iloc[-1]), 2)
    if hit is not None:
        hrs = int((hit - t0) / pd.Timedelta(hours=1))
        out.update({"狀態": "達成", "達成時間": hit.strftime("%Y-%m-%dT%H:%M"), "歷時": hrs,
                    "說明": f"{hrs} 小時後達成目標"})
    elif now >= deadline:
        out.update({"狀態": "未達成", "說明": f"{g['deadline']} 小時期限內未達成"})
    else:
        left = int((deadline - now) / pd.Timedelta(hours=1))
        out.update({"狀態": "追蹤中", "說明": f"尚未達成，期限還剩 {left} 小時"})
    if executed is False:
        out["樣本"] = "不計入成效樣本（現場未執行，環境變化不能歸功於動作）"
    elif executed:
        out["樣本"] = "計入成效樣本"
    return out


def rule_stats(engine, reviews: list):
    """每條規則（場域＋設備）的審核統計，以及對應動作的成效驗證進度。"""
    effects = {e["action"]: e for e in engine.verify["effects"]}
    me = engine.motor_effect
    effects["馬達運轉"] = {"action": "馬達運轉", "n": me["n"], "effect": me["level_gap_same_hour"], "se": None,
                       "verified": me["verified"], "note": "停機時 DO 比同時段運轉時低多少（負值＝運轉較好）"}
    latest = {}
    for r in sorted(reviews, key=lambda r: r["id"]):
        latest[r["decision_id"]] = r  # 同一決策多次審核只算最後一次
    rules = {}
    for r in latest.values():
        d = r["decision"]
        key = (r["site_id"], d.get("設備"))
        x = rules.setdefault(key, {"site_id": r["site_id"], "場域": d.get("場域"), "設備": d.get("設備"),
                                   "核准": 0, "駁回": 0, "駁回原因": []})
        if r["action"] == "approve":
            x["核准"] += 1
        else:
            x["駁回"] += 1
            if r.get("note"):
                x["駁回原因"].append(r["note"])
    out = []
    for x in rules.values():
        n = x["核准"] + x["駁回"]
        x["駁回率"] = round(x["駁回"] / n, 3) if n else 0
        x["建議檢討"] = n >= REVIEW_FLAG_MIN and x["駁回率"] >= 0.5
        g = goal_for({"site_id": x["site_id"], "設備": x["設備"], "動作": ""})
        e = effects.get(g["effect_key"]) if g and g.get("effect_key") else None
        if e:
            x["成效"] = {"動作": e["action"], "樣本數": e["n"], "門檻": MIN_SAMPLES, "效果": e.get("effect"),
                       "標準誤": e.get("se"), "已驗證": bool(e["verified"])}
        out.append(x)
    # 沒被審核過但有成效資料的動作也列出，讓使用者看到驗證進度
    listed = {x.get("成效", {}).get("動作") for x in out}
    for k, e in effects.items():
        if k not in listed:
            out.append({"site_id": "expert" if k == "馬達運轉" else "sheep", "場域": None, "設備": k,
                        "核准": 0, "駁回": 0, "駁回率": 0, "建議檢討": False, "駁回原因": [],
                        "成效": {"動作": k, "樣本數": e["n"], "門檻": MIN_SAMPLES, "效果": e.get("effect"),
                               "標準誤": e.get("se"), "已驗證": bool(e["verified"])}})
    return {"rules": out, "門檻": {"樣本數": MIN_SAMPLES, "單尾z": Z_ONE_SIDED, "檢討最少審核次數": REVIEW_FLAG_MIN}}
