"""05 執行回饋：從羊舍的實際設備動作紀錄，驗證「動作 → 環境變化」的成效，
並把結果寫回成效資料庫（effect_db），供決策引擎下一輪使用（持續學習）。"""
import json

from .config import MIN_SAMPLES, Z_ONE_SIDED
from pathlib import Path

import numpy as np
import pandas as pd

FANS = ["風扇-03", "風扇-04", "風扇-07", "風扇-08"]
EFFECT_DB = Path(__file__).resolve().parent.parent / "outputs" / "effect_db.json"


def device_status(raw: pd.DataFrame) -> dict:
    """狀態碼：0=停止、1=運轉/動作中、2/4=設備回報異常碼（本資料中所有設備同時出現，判為控制器層級異常）。"""
    dev = [c for c in raw.columns if ("風扇" in c or "帆布" in c or "遮陽" in c)]
    codes = raw[dev]
    abnormal = (codes >= 2).any(axis=1)
    return {"columns": dev, "abnormal_hours": raw.index[abnormal],
            "fan_on": (raw[FANS] == 1).sum(axis=1)}


def action_effect(raw: pd.DataFrame, df: pd.DataFrame, mask: pd.Series, name: str, target="thi"):
    """配對比較：動作小時的 ΔTHI(t-1→t+1) 減去「同時段、未動作」小時的平均 ΔTHI。
    同時段比較可扣除日夜週期本身造成的升降溫。"""
    y = df[target]
    d = y.shift(-1) - y.shift(1)
    hours = df.index.hour
    base = d[~mask].groupby(hours[~mask]).mean()
    on = d[mask].dropna()
    if len(on) == 0:
        return {"action": name, "n": 0, "effect": None, "verified": False}
    diff = on - base.reindex(on.index.hour).values
    diff = diff.dropna()
    eff = float(diff.mean()) if len(diff) else None
    se = float(diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else None
    verified = bool(len(diff) >= MIN_SAMPLES and eff is not None and se is not None and eff + Z_ONE_SIDED * se < 0)
    return {"action": name, "n": int(len(diff)), "effect": eff, "se": se, "verified": verified,
            "target": target, "note": "負值=動作後 THI 比同時段平常多降（或少升）"}


def sheep_effects(site: dict):
    """依傳入的資料（決策引擎回放時已切到當下時間）計算風扇、遮陽、帆布的成效，不偷看未來。"""
    raw, df = site["raw"], site["clean"]
    st = device_status(raw)
    ok = ~raw.index.isin(st["abnormal_hours"])
    fans_on = (raw[FANS] == 1).any(axis=1) & ok
    shade_on = (raw["南遮陽張 25/26"] == 1) & ok
    canvas_on = (raw["南右帆布張 23/24"] == 1) & ok
    effects = [action_effect(raw, df, fans_on, "風扇開啟"),
               action_effect(raw, df, shade_on, "南遮陽張開"),
               action_effect(raw, df, canvas_on, "南右帆布張開")]
    n_on = (raw[FANS] == 1).sum(axis=1)[fans_on]
    if len(n_on):   # 開啟時平均同時開幾台、全部同時開的比例 → 推算每台效果用
        effects[0]["fans_mean"] = float(n_on.mean())
        effects[0]["all_on_share"] = float((n_on == len(FANS)).mean())
        effects[0]["fan_counts"] = {int(k): int(v) for k, v in n_on.value_counts().sort_index().items()}
    return effects, st, fans_on, shade_on, canvas_on


def verify_sheep(site: dict) -> dict:
    effects, st, fans_on, shade_on, canvas_on = sheep_effects(site)
    db = {e["action"]: e for e in effects}
    EFFECT_DB.parent.mkdir(exist_ok=True)
    EFFECT_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    return {"effects": effects, "abnormal_hours": [str(t) for t in st["abnormal_hours"]],
            "fan_on_hours": int(fans_on.sum()), "shade_on_hours": int(shade_on.sum()),
            "canvas_on_hours": int(canvas_on.sum())}


def replay_sheep(site: dict, rule) -> dict:
    """閉環回放：逐小時把歷史資料餵給決策規則，與現場實際操作對照。"""
    raw, df = site["raw"], site["clean"]
    rec = df.apply(lambda r: rule(r), axis=1)
    actual = (raw[FANS] == 1).sum(axis=1)
    thi = df["thi"]
    out = pd.DataFrame({"thi": thi, "rec_fans": rec, "act_fans": actual, "addon": raw.get("成羊區 加開")})
    need = out["rec_fans"] > 0
    daily = out.resample("D").agg({"thi": "max", "rec_fans": lambda x: int((x > 0).sum()),
                                    "act_fans": lambda x: int((x > 0).sum())})
    return {"hours": int(len(out)),
            "hours_need": int(need.sum()),
            "hours_actual": int((out["act_fans"] > 0).sum()),
            "hours_need_and_actual": int((need & (out["act_fans"] > 0)).sum()),
            "hours_thi_danger": int((thi >= 79).sum()), "hours_thi_emergency": int((thi >= 84).sum()),
            "addon_flag_hours": int((out["addon"] == 1).sum()) if "addon" in out else None,
            "daily": daily, "series": out}


def verify_expert(site: dict) -> dict:
    """馬達停機對 DO 的影響：停機小時的 ΔDO 與同時段運轉小時比較。
    effect < 0 且顯著 → 證實「運轉打氣能維持 DO」，此動作列為已驗證。"""
    df = site["df"]
    online = df["power"].fillna(0) > 5
    stop = (df["current"] < 0.5) & online
    run = (df["current"] >= 0.5) & online
    d = df["do1"].shift(-1) - df["do1"].shift(1)
    hours = df.index.hour
    base = d[run].groupby(hours[run]).mean()
    on = d[stop].dropna()
    diff = (on - base.reindex(on.index.hour).values).dropna()
    eff = float(diff.mean()) if len(diff) else None
    se = float(diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else None
    # 同時段配對的 DO 水準比較（排除日夜差異）
    lvl_stop = df.loc[stop, "do1"]
    lvl_run_same = df.loc[run, "do1"].groupby(hours[run]).median().reindex(lvl_stop.index.hour).values
    level_gap = float(np.nanmedian(lvl_stop.values - lvl_run_same)) if len(lvl_stop) else None
    days = (df.index[-1] - df.index[0]).days or 1
    return {"n": int(len(diff)), "effect": eff, "se": se, "level_gap_same_hour": level_gap,
            "verified": bool(level_gap is not None and level_gap < -0.1 and len(lvl_stop) >= MIN_SAMPLES),
            "do_stop": float(df.loc[stop, "do1"].median()), "do_run": float(df.loc[run, "do1"].median()),
            "stop_hours_per_day": float(stop.sum() / days)}
