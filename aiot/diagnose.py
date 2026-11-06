"""通用感測器診斷：從資料本身偵測感測器問題，不寫死任何日期或數值。

每個偵測器只使用傳入的資料（決策引擎回放時已切到「當下時間」以前），
回傳「發現」清單，每筆含：類型、變數、時間、證據（全部由資料計算）。

偵測項目：
  stuck_value      卡值／無反應：長時間幾乎都是同一個數值（常見為 0）
  step_drift       階躍漂移：日中位數突然跳升並持續，之後逐日緩降 → 零點漂移特徵；並與鄰站比對
  restart_spike    重啟假峰值：斷線恢復後第一筆異常偏高，下一小時即回落
  impossible       不可能讀值：超出物理合理範圍；同一數值重複出現 → 量測上限飽和；只有一小時 → 單點極端值
"""
import numpy as np
import pandas as pd

from .config import VALID_RANGE


def _range(cfg, col):
    return VALID_RANGE.get(cfg.get("range_of", {}).get(col, col))


def _fmt_t(ts):
    return f"{ts.month}/{ts.day} {ts.hour} 時"


def _fmt_d(ts):
    return f"{ts.month}/{ts.day}"


def _num(v):
    v = float(v)
    return f"{v:.0f}" if abs(v) >= 100 else (f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}")


# ------------------------------------------------------------------ 卡值／無反應
def stuck_value(s: pd.Series, min_hours=72, share=0.9):
    """有資料的小時中，超過 share 比例是同一個數值 → 卡值。"""
    v = s.dropna()
    if len(v) < min_hours:
        return None
    counts = v.round(4).value_counts()
    top, n = counts.index[0], int(counts.iloc[0])
    if n / len(v) < share:
        return None
    return {"類型": "卡值", "數值": float(top), "比例": n / len(v), "時數": len(v),
            "開始": v.index[0], "結束": v.index[-1]}


# ------------------------------------------------------------------ 階躍漂移
def step_drift(s: pd.Series, first_cut: float, peers: dict = None, temp: pd.Series = None):
    """日中位數的最大單日跳升 ≥ 第一級門檻、且大於跳升前 14 天波動的 5 倍，並持續 3 天以上 → 階躍。
    跳升後日中位數若逐日下降 → 零點漂移特徵（電化學感測器常見）。
    peers：鄰站同一變數；鄰站同期也跳升 → 可能是區域事件或同批感測器，不直接判定為單站漂移。"""
    d = s.resample("D").median().dropna()
    if len(d) < 10:
        return None
    jump = d.diff()
    day = jump.idxmax()
    size = float(jump.max())
    if not np.isfinite(size) or size < first_cut:
        return None
    before = d.loc[:day].iloc[-15:-1]
    spread = float((before.quantile(0.9) - before.quantile(0.1))) if len(before) >= 3 else 0.0
    if size < 5 * max(spread, 1e-6):
        return None
    after = d.loc[day:]
    if len(after) < 3 or float(after.iloc[:3].median()) < d.loc[day] * 0.5:
        return None                                  # 只跳一天就回來 → 不是階躍（交給其他偵測）
    slope = float(np.polyfit(np.arange(len(after)), after.values, 1)[0]) if len(after) >= 5 else 0.0
    decay = slope < 0 and float(after.iloc[-1]) < float(after.iloc[0]) * 0.8
    months = s.loc[day - pd.Timedelta(days=31):].resample("MS").median().dropna()
    out = {"類型": "階躍", "日期": day, "前": float(d.shift(1).loc[day]), "後": float(d.loc[day]),
           "現在": float(after.iloc[-1]), "緩降": decay, "月中位數": months, "鄰站同步": [], "鄰站無變化": []}
    for name, ps in (peers or {}).items():
        pd_ = ps.loc[:s.index[-1]].resample("D").median().dropna()
        win = pd_.loc[day - pd.Timedelta(days=3):day + pd.Timedelta(days=3)]
        prev = pd_.loc[:day - pd.Timedelta(days=4)].iloc[-14:]
        if len(win) < 2 or len(prev) < 3:
            continue
        if (pd_ == 0).mean() > 0.9:
            continue                                 # 鄰站本身失效（長期為 0），不拿來比對
        if float(win.max() - prev.median()) >= size * 0.5:
            out["鄰站同步"].append(name)
        else:
            out["鄰站無變化"].append(name)
    if temp is not None:
        x = pd.concat([s.loc[day:], temp.loc[day:]], axis=1).dropna()
        if len(x) >= 48:
            out["溫度相關"] = float(x.corr().iloc[0, 1])
    return out


# ------------------------------------------------------------------ 重啟假峰值
def restart_spike(df: pd.DataFrame, cols, cuts: dict, min_gap=2):
    """整列缺值（斷線）至少 min_gap 小時後恢復的第一筆，比前 24 小時中位數高 5 倍以上且超過第一級門檻，
    下一小時又回落到 30% 以下 → 暖機假峰值。"""
    gap = df.isna().all(axis=1)
    runs = (gap != gap.shift()).cumsum()
    found = []
    for _, g in gap[gap].groupby(runs[gap]):
        if len(g) < min_gap:
            continue
        i = df.index.get_indexer([g.index[-1]])[0] + 1
        if i + 1 >= len(df):
            continue
        t0, t1 = df.index[i], df.index[i + 1]
        for k in cols:
            if k not in df:
                continue
            v0, v1 = df[k].iloc[i], df[k].iloc[i + 1]
            ref = df[k].loc[:g.index[0]].iloc[-25:-1].median()
            cut = (cuts.get(k) or [0])[0]
            if pd.notna(v0) and pd.notna(v1) and pd.notna(ref) and v0 > max(5 * ref, cut) and v1 < 0.3 * v0:
                found.append({"類型": "重啟", "變數": k, "斷線開始": g.index[0], "斷線時數": len(g),
                              "恢復": t0, "峰值": float(v0), "下一小時": float(v1), "平常": float(ref)})
    return found


# ------------------------------------------------------------------ 不可能讀值
def impossible(df: pd.DataFrame, cfg: dict, cols, merge_hours=48):
    """超出物理合理範圍的讀值。間隔 48 小時內的視為同一事件；整列同時為 0 的時段屬於斷線，不在此列。
    事件分類：只有 1 小時且前後正常 → 單點；同一數值重複出現且超過上限 → 飽和；其他 → 超出範圍。"""
    keys = [k for k in cfg["key"] if k in df]
    outage = (df[keys].fillna(0) == 0).mean(axis=1) >= 0.6
    found = []
    for k in cols:
        rg = _range(cfg, k)
        if k not in df or not rg:
            continue
        lo, hi = rg
        bad = ((df[k] < lo) | (df[k] > hi)) & ~outage
        hits = df.index[bad]
        if not len(hits):
            continue
        groups, cur = [], [hits[0]]
        for t in hits[1:]:
            if t - cur[-1] > pd.Timedelta(hours=merge_hours):
                groups.append(cur)
                cur = []
            cur.append(t)
        groups.append(cur)
        for g in groups:
            vals = df.loc[g, k]
            top = vals.round(3).value_counts()
            repeat = len(vals) >= 2 and top.iloc[0] / len(vals) >= 0.8
            i0, i1 = df.index.get_indexer([g[0], g[-1]])
            nb = df[k].iloc[[j for j in (i0 - 1, i1 + 1) if 0 <= j < len(df)]].dropna()
            nb_ok = bool(len(nb) and nb.between(lo, hi).all())
            if len(g) == 1 and nb_ok:
                kind = "單點"
            elif repeat and float(top.index[0]) > hi:
                kind = "飽和"
            else:
                kind = "超出範圍"
            found.append({"類型": kind, "變數": k, "開始": g[0], "結束": g[-1], "時數": len(g),
                          "數值": float(top.index[0]) if kind == "飽和" else float(vals.iloc[0]),
                          "最大": float(vals.max()), "最小": float(vals.min()), "範圍": rg,
                          "前後": [float(x) for x in nb.values]})
    return sorted(found, key=lambda f: f["開始"])


# ------------------------------------------------------------------ 轉成診斷文字
def describe(f, label, others=None):
    """把一筆發現轉成（問題, 可能原因, 判斷證據, 可信度）。label：變數中文名。"""
    t = f["類型"]
    if t == "卡值":
        return (f"{label} 讀值無反應", "感測器故障、接線或平均值計算設定錯誤",
                f"{_fmt_d(f['開始'])}–{_fmt_d(f['結束'])} 共 {f['時數']} 小時，{f['比例']:.0%} 為 {_num(f['數值'])}", "高")
    if t == "階躍":
        ms = "、".join(f"{d.month}月 {v:.1f}" for d, v in f["月中位數"].items())
        trend = "逐日緩降" if f["緩降"] else ("已回落" if f["現在"] < 0.3 * f["後"] else "持續偏高")
        ev = (f"日中位數 {_fmt_d(f['日期'])} 前一天 {_num(f['前'])}，當天跳到 {_num(f['後'])}"
              f"，之後{trend}，現在 {_num(f['現在'])}；月中位數 {ms}")
        if f.get("超出量程"):
            ev += f"；期間 {f['超出量程']} 小時超過合理範圍上限（已自動排除）"
        if f["鄰站無變化"]:
            ev += f"；同期 {'、'.join(f['鄰站無變化'])} 沒有同步上升"
        if f["鄰站同步"]:
            ev += f"；同期 {'、'.join(f['鄰站同步'])} 也上升"
        if f.get("溫度相關") is not None and f["溫度相關"] > 0.3:
            ev += f"；跳升後讀值與氣溫相關 r={f['溫度相關']:.2f}（溫度干擾特徵）"
        if f["鄰站同步"]:
            return (f"{label} 讀值階躍上升（多站同步）", "區域性排放事件，或同批感測器同時漂移；需現場比對", ev, "中")
        cause = "感測器零點漂移（重新上電或更換後未校正）；或局部排放源" if f["緩降"] else "感測器偏移或局部排放源"
        return (f"{label} 讀值階躍上升{'後緩降' if f['緩降'] else ''}", cause, ev, "中")
    if t == "重啟":
        return (f"{label} 斷線恢復後尖峰", "斷電重啟後感測器暖機假峰值",
                f"{_fmt_t(f['斷線開始'])}起斷線 {f['斷線時數']} 小時，{_fmt_t(f['恢復'])}恢復即出現 {_num(f['峰值'])}"
                f"（平常約 {_num(f['平常'])}），下一小時回落到 {_num(f['下一小時'])}"
                + (f"；{others}" if others else ""), "高")
    lo, hi = f["範圍"]
    span = (_fmt_t(f["開始"]) if f["開始"] == f["結束"] else
            f"{_fmt_t(f['開始'])}–{_fmt_t(f['結束'])}")
    if t == "飽和":
        return (f"{label} 量測上限飽和", "感測器超出量程（實際濃度過高或感測器異常）",
                f"{span}共 {f['時數']} 小時固定為 {_num(f['數值'])}（合理範圍上限 {_num(hi)}）；已自動排除", "中")
    if t == "單點":
        high = f["數值"] > hi
        return (f"{label} 單點極端值 {_num(f['數值'])}",
                "感測器量測上限飽和或緊鄰排放源" if high else "感測器瞬間故障（物理上不可能的讀值）",
                f"{span} 讀值 {_num(f['數值'])}，超出合理範圍 {_num(lo)}–{_num(hi)}，"
                f"前後小時 {'、'.join(_num(x) for x in f['前後'])} 正常，只持續 1 小時；已自動排除"
                + (f"；同一小時 {others}" if others else ""), "高" if not high else "中")
    return (f"{label} 讀值超出合理範圍", "感測器故障或量程設定錯誤",
            f"{span}共 {f['時數']} 小時，讀值 {_num(f['最小'])}–{_num(f['最大'])}"
            f"（合理範圍 {_num(lo)}–{_num(hi)}）；已自動排除", "中")


def sensor_findings(df, cfg, labels, gas=(), peers=None, cuts=None, temp=None, restart_cols=()):
    """組合所有偵測器，回傳 [(發現, 診斷 tuple)]，同一時間的單點極端值合併成一筆。"""
    cuts = cuts or {}
    out, steps = [], {}
    for k in gas:
        if k not in df:
            continue
        st = stuck_value(df[k])
        if st:
            st["變數"] = k
            out.append((st, describe(st, labels.get(k, k))))
            continue
        f = step_drift(df[k], (cuts.get(k) or [0])[0], (peers or {}).get(k), temp)
        if f:
            f["變數"] = k
            steps[k] = f
    imp = impossible(df, cfg, [k for k in cfg["key"] if k in df])
    for k, f in steps.items():   # 階躍期間的超量程讀值併入階躍事件
        inside = [x for x in imp if x["變數"] == k and f["日期"] <= x["開始"] <= f["日期"] + pd.Timedelta(days=14)]
        f["超出量程"] = sum(x["時數"] for x in inside)
        imp = [x for x in imp if x not in inside]
        out.append((f, describe(f, labels.get(k, k))))
    for f in restart_spike(df, restart_cols, cuts):
        out.append((f, describe(f, labels.get(f["變數"], f["變數"]))))
    singles = {}
    for f in imp:
        if f["類型"] == "單點":
            singles.setdefault(f["開始"], []).append(f)
        else:
            out.append((f, describe(f, labels.get(f["變數"], f["變數"]))))
    for t, fs in singles.items():
        main = fs[0]
        others = "、".join(f"{labels.get(x['變數'], x['變數'])} {_num(x['數值'])}" for x in fs[1:])
        p, c, e, conf = describe(main, labels.get(main["變數"], main["變數"]), others or None)
        if len(fs) > 1:
            p = f"單點極端值（{'、'.join(labels.get(x['變數'], x['變數']) for x in fs)}）"
        out.append((main, (p, c, e, conf)))
    return out
