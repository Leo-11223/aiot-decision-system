"""05 執行回饋：從現場實際的設備動作紀錄，驗證「動作 → 環境變化」的成效。

決策引擎每個時間點都用「當下以前」的紀錄重新計算成效（sheep_effects、verify_expert），
樣本達驗證標準後動作才升級為自動執行；新資料進來會自動納入（持續學習）。
effect_db.json 只是批次執行（run.py）時輸出的成效報表，網頁版不讀它。"""
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


def verify_sheep(site: dict, write: bool = False) -> dict:
    """write=True 時輸出成效報表 effect_db.json（批次執行 run.py 用；網頁版不寫檔）。"""
    effects, st, fans_on, shade_on, canvas_on = sheep_effects(site)
    if write:
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


def verify_expert(site: dict, days_window=3) -> dict:
    """馬達停機對 DO 的影響（只用傳入的資料，即當下以前）。

    做法（比照風扇的驗證標準）：
      - DO 用三支可信探棒的中位數（共識值），不用單一探棒。
      - 每個停機小時，與「前後 3 天內同一時段、馬達運轉」的共識 DO 中位數比較，排除季節與日夜差異。
      - 以「日」為單位平均後再算標準誤（同一天的小時彼此相關，不能當獨立樣本）。
      - 已驗證 = 停機 ≥ MIN_SAMPLES 小時、至少 5 天，且單尾 95% 顯著（效果 + 1.645×標準誤 < 0）。"""
    df, c = site["df"], site["clean"]
    do_cols = [k for k in ("do1", "do2", "do3") if k in c]
    cons = c[do_cols].median(axis=1)
    online = df["power"].fillna(0) > 5
    stop = (df["current"] < 0.5) & online & cons.notna()
    run = (df["current"] >= 0.5) & online & cons.notna()
    hours = df.index.hour
    diffs = {}
    run_idx = df.index[run]
    for ts in df.index[stop]:
        win = run_idx[(run_idx >= ts - pd.Timedelta(days=days_window)) & (run_idx <= ts + pd.Timedelta(days=days_window))
                      & (run_idx.hour == ts.hour)]
        if len(win) >= 2:
            diffs[ts] = float(cons[ts] - cons[win].median())
    d = pd.Series(diffs, dtype=float)
    daily = d.groupby(d.index.date).mean() if len(d) else pd.Series(dtype=float)
    n_days = int(len(daily))
    eff = float(daily.mean()) if n_days else None
    se = float(daily.std(ddof=1) / np.sqrt(n_days)) if n_days > 1 else None
    verified = bool(len(d) >= MIN_SAMPLES and n_days >= 5 and eff is not None and se is not None
                    and eff + Z_ONE_SIDED * se < 0)
    days = (df.index[-1] - df.index[0]).days or 1
    return {"n": int(len(d)), "n_days": n_days, "effect": eff, "se": se,
            "level_gap_same_hour": eff,      # 舊欄位名稱相容：停機時 DO 比相鄰日同時段運轉低多少
            "verified": verified,
            "method": f"共識 DO，與前後 {days_window} 天同時段運轉比較，以日為單位檢定",
            "do_stop": float(cons[stop].median()) if stop.any() else None,
            "do_run": float(cons[run].median()) if run.any() else None,
            "stop_hours_per_day": float(stop.sum() / days)}
