"""02 大數據：匯入、清洗、感測器可信度、歷史基準。"""
import io
from pathlib import Path

import numpy as np
import pandas as pd

from .config import SITES, VALID_RANGE, AIR_PEERS, AIR_PEER_TOL

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def read_raw(path: Path) -> tuple[pd.DataFrame, dict]:
    """讀取平台匯出的 UTF-16 Tab 分隔檔；表頭前若有 HH/HI 等設定列一併回傳。"""
    text = path.read_bytes().decode("utf-16")
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("日期"))
    meta = {}
    for l in lines[:start]:
        parts = [p for p in l.split("\t") if p.strip()]
        if len(parts) >= 2:
            meta[parts[0]] = parts[1]
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep="\t")
    df.columns = [c.split(" - ", 1)[-1].strip() for c in df.columns]
    ts = pd.to_datetime(df["日期"].astype(str)) + pd.to_timedelta(df["小時"].astype(int), unit="h")
    df = df.drop(columns=["日期", "小時"]).set_index(ts).sort_index()
    df.index.name = "ts"
    df = df[~df.index.duplicated()]
    return df.apply(pd.to_numeric, errors="coerce"), meta


def load_site(site_id: str) -> dict:
    cfg = SITES[site_id]
    raw, meta = read_raw(DATA_DIR / cfg["file"])
    full = pd.date_range(raw.index.min(), raw.index.max(), freq="h")
    raw = raw.reindex(full)
    raw.index.name = "ts"
    df = raw.rename(columns=cfg["rename"])
    return {"id": site_id, "cfg": cfg, "raw": raw, "df": df, "meta": meta}


# ------------------------------------------------------------------ 感測器可信度
def _range_key(cfg, col):
    return cfg.get("range_of", {}).get(col, col)


def sensor_trust(site: dict) -> pd.DataFrame:
    """每個關鍵感測器、每小時一個 0–100 的可信度，並記錄扣分原因。

    扣分規則（可解釋、可稽核）：
      缺值 / 斷線(整列為 0) / 超出物理範圍 → 0
      突波（一階差分 robust z > 6）        → -50
      卡值（連續 12 小時完全相同）          → -40
      備援不一致（與同類探棒中位數差過大）   → -45
    """
    cfg, df = site["cfg"], site["df"]
    keys = [k for k in cfg["key"] if k in df.columns]
    trust = pd.DataFrame(100.0, index=df.index, columns=keys)
    reason = pd.DataFrame("", index=df.index, columns=keys)

    def hit(mask, col, penalty, why, floor=False):
        mask = mask.fillna(False)
        if floor:
            trust.loc[mask, col] = 0
        else:
            trust.loc[mask, col] = (trust.loc[mask, col] - penalty).clip(lower=0)
        reason.loc[mask, col] = reason.loc[mask, col] + why + ";"

    env_cols = [k for k in keys if k not in ("current", "power", "pf", "battery", "batt_v", "kwh")]
    offline = (df[env_cols].fillna(0) == 0).all(axis=1) | df[env_cols].isna().all(axis=1)

    for c in keys:
        s = df[c]
        hit(s.isna(), c, 0, "缺值", floor=True)
        hit(offline & s.notna(), c, 0, "整列為0(斷線)", floor=True)
        rng = VALID_RANGE.get(_range_key(cfg, c))
        if rng:
            hit((s < rng[0]) | (s > rng[1]), c, 0, "超出物理範圍", floor=True)
        # 突波：與「過去 30 天」的變化量分布比較（只用當下以前的資料，回放時不偷看未來）
        d = s.diff()
        med = d.rolling(720, min_periods=48).median().shift(1)
        mad = (d - med).abs().rolling(720, min_periods=48).median().shift(1)
        hit(((d - med).abs() / (1.4826 * mad.where(mad > 0))) > 6, c, 50, "突波")
        if c not in ("battery", "batt_v"):  # 電池充飽維持 100% 屬正常
            stuck = s.rolling(12).apply(lambda x: np.nanmax(x) - np.nanmin(x) == 0, raw=True) == 1
            saturated = (s >= 99.5) & (c in ("rh",))  # 濕度飽和 100% 屬正常
            hit(stuck & s.notna() & ~offline & ~saturated & (s == 0), c, 40, "長時間讀值為0(疑似未感應)")
            hit(stuck & s.notna() & ~offline & ~saturated & (s != 0), c, 40, "卡值12h")

    # 同類探棒備援比對（專家系統 DO/pH/ORP/SC 各三支）
    for g, cols in cfg.get("groups", {}).items():
        tol = cfg["group_tol"][g]
        med = df[cols].median(axis=1)  # 三支表決：與三支中位數差太多者判定可疑
        for c in cols:
            hit((df[c] - med).abs() > tol, c, 45, f"與其他{g}探棒不一致")
    # 單一設備內的冗餘量測（例：羊舍 ID4 溫度 vs 成羊區平均溫度）
    for a, b, tol in cfg.get("redundant", []):
        if a in df and b in df:
            m = (df[a] - df[b]).abs() > tol
            for c in (a, b):
                if c in keys:
                    hit(m, c, 20, f"{a}/{b}冗餘不一致")
    # 羊舍 THI 與溫濕度重算值互相驗證（平台公式：THI = 1.8T+32 − (0.55−0.0055RH)(1.8T−26)）
    if "thi" in keys and {"temp_avg", "rh_avg"} <= set(df.columns):
        calc = thi_formula(df["temp_avg"], df["rh_avg"])
        hit((calc - df["thi"]).abs() > 1.0, "thi", 45, "與溫濕度重算THI不一致")
    return trust, reason


def thi_formula(t, rh):
    return 1.8 * t + 32 - (0.55 - 0.0055 * rh) * (1.8 * t - 26)


def peer_check(sites: dict) -> None:
    """空品三站同時刻互相比對：與另兩站中位數差超過容許值的單站讀值扣分。"""
    for var, tol in AIR_PEER_TOL.items():
        frame = pd.concat({s: sites[s]["df"][var] for s in AIR_PEERS if var in sites[s]["df"]}, axis=1)
        for s in frame.columns:
            others = frame.drop(columns=s).median(axis=1)
            bad = ((frame[s] - others).abs() > tol) & others.notna()
            t, r = sites[s]["trust"], sites[s]["reason"]
            bad = bad.reindex(t.index).fillna(False)
            if var in t:
                t.loc[bad, var] = (t.loc[bad, var] - 45).clip(lower=0)
                r.loc[bad, var] = r.loc[bad, var] + "與鄰站差異過大;"


# ------------------------------------------------------------------ 清洗後資料 + 基準
def clean(site: dict) -> pd.DataFrame:
    """可信度 < 50 的讀值視為無效 → NaN；短缺口（≤3h）沿用前一筆有效值，長缺口保留空白。
    不用前後內插：內插會用到缺口之後（未來）的讀值，回放時等於偷看未來。"""
    df = site["df"].copy()
    t = site["trust"]
    for c in t.columns:
        df.loc[t[c] < 50, c] = np.nan
        df[c] = df[c].ffill(limit=3)
    return df


def baseline(clean_df: pd.DataFrame, cols, train_frac=0.7) -> dict:
    """以前 70% 期間建立「每小時時段」的中位數與 MAD 基準（日夜週期）。"""
    n = int(len(clean_df) * train_frac)
    tr = clean_df.iloc[:n]
    out = {}
    for c in cols:
        if c not in tr:
            continue
        g = tr[c].groupby(tr.index.hour)
        med = g.median()
        mad = g.apply(lambda x: (x - x.median()).abs().median()) * 1.4826
        floor = max(float(tr[c].std() or 0) * 0.1, 1e-6)
        out[c] = {"median": med.reindex(range(24)).ffill().bfill(),
                  "scale": mad.reindex(range(24)).ffill().bfill().clip(lower=floor)}
    return out


def load_all() -> dict:
    sites = {sid: load_site(sid) for sid in SITES}
    for s in sites.values():
        s["trust"], s["reason"] = sensor_trust(s)
    peer_check(sites)
    for s in sites.values():
        s["clean"] = clean(s)
        s["baseline"] = baseline(s["clean"], s["cfg"]["key"])
    return sites
