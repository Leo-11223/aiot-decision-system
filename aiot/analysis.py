"""03 AI 分析：狀態辨識（指數＋分級）、異常偵測、趨勢預測、設備故障機率。"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import mean_absolute_error, roc_auc_score

LEVEL_NAME = ["正常", "警戒", "危險", "緊急"]

# 梯度提升模型參數：較小的樹（60 輪、每棵 15 葉、葉節點至少 20 筆）。
# 實測比 200 輪的大模型更準（較不易過度學習），訓練快約 5 倍、檔案小約 5 倍，適合每週重訓。
HGB = dict(max_iter=60, learning_rate=0.1, max_leaf_nodes=15, min_samples_leaf=20, random_state=0)


# ------------------------------------------------------------------ 狀態辨識
def level_of(value, cuts, lower_is_bad=False):
    if value is None or pd.isna(value):
        return None
    if lower_is_bad:
        return int(sum(value < c for c in cuts))
    return int(sum(value >= c for c in cuts))


def _lin(x, x0, y0, x1, y1):
    return float(np.clip(y0 + (x - x0) * (y1 - y0) / (x1 - x0), min(y0, y1), max(y0, y1)))


def wqi(do=None, ph=None, ec=None):
    """簡化水質指數 0–100（越高越好）：DO 權重 0.5、pH 0.25、EC 0.25（缺項自動重新加權）。"""
    parts = []
    if do is not None and not pd.isna(do):
        parts.append((0.5, _lin(do, 0, 0, 6.5, 100)))
    if ph is not None and not pd.isna(ph):
        sub = 100 if 6.5 <= ph <= 8.5 else (_lin(ph, 4.5, 0, 6.5, 100) if ph < 6.5 else _lin(ph, 8.5, 100, 10.5, 0))
        parts.append((0.25, sub))
    if ec is not None and not pd.isna(ec):
        parts.append((0.25, _lin(ec, 750, 100, 3000, 0)))
    if not parts:
        return None
    w = sum(p[0] for p in parts)
    return round(sum(p[0] * p[1] for p in parts) / w, 1)


# 環境部 2025/1/1 起新版 AQI 之 PM2.5（24h 平均）分段
_AQI_PM25 = [(0, 12.4, 0, 50), (12.5, 30.4, 51, 100), (30.5, 50.4, 101, 150), (50.5, 125.4, 151, 200),
             (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 400), (325.5, 500.4, 401, 500)]


def aqi_pm25(c):
    """AQI 的 PM2.5 子指標（以 24 小時平均濃度計）。"""
    if c is None or pd.isna(c):
        return None
    for lo, hi, ilo, ihi in _AQI_PM25:
        if c <= hi:
            return int(round(ilo + (c - lo) * (ihi - ilo) / (hi - lo)))
    return 500


# ------------------------------------------------------------------ 異常偵測
def detect_anomalies(clean_df, cols, base, train_frac=0.7, contamination=0.02):
    """兩層偵測：
       ① 單變數：相對於「同時段歷史基準」的 robust z 分數（|z|>4 為異常）
       ② 多變數：IsolationForest（學習變數之間的正常關係，抓組合異常）
    回傳逐時分數表與合併後的異常事件。"""
    cols = [c for c in cols if c in clean_df and clean_df[c].notna().mean() > 0.1 and c in base]
    hours = clean_df.index.hour
    if not cols or clean_df[cols].notna().all(axis=1).sum() < 100:
        # 有效資料太少（例：完整率過低的站），不做模型偵測
        empty = pd.Series(False, index=clean_df.index)
        return {"z": pd.DataFrame(index=clean_df.index), "score": empty.astype(float), "flag": empty,
                "threshold": None, "events": [], "skipped": "有效資料不足"}
    z = pd.DataFrame(index=clean_df.index)
    for c in cols:
        z[c] = (clean_df[c] - base[c]["median"].reindex(hours).values) / base[c]["scale"].reindex(hours).values
    X = z.fillna(0).clip(-20, 20)
    valid = clean_df[cols].notna().mean(axis=1) >= 0.6
    n = int(len(X) * train_frac)
    iso = IsolationForest(n_estimators=200, contamination=contamination, random_state=0)
    iso.fit(X[valid].iloc[: max(50, int(valid[:n].sum()))])
    score = pd.Series(-iso.score_samples(X), index=X.index)
    thr = float(np.quantile(score[valid].iloc[:n], 1 - contamination)) if valid[:n].sum() > 50 else float(score.quantile(0.98))
    flag = ((score > thr) | (z.abs() > 4).any(axis=1)) & valid
    events = _events(flag, z)
    return {"z": z, "score": score, "flag": flag, "threshold": thr, "events": events}


def _events(flag, z):
    """合併連續異常小時為事件，記錄最異常的變數與方向。"""
    events, cur = [], None
    for ts, f in flag.items():
        if f:
            row = z.loc[ts].abs().dropna()
            top = row.sort_values(ascending=False).head(3)
            if cur and (ts - cur["end"]) <= pd.Timedelta(hours=1):
                cur["end"] = ts
                cur["peak"] = max(cur["peak"], float(row.max()) if len(row) else 0)
                for k, v in top.items():
                    cur["vars"][k] = max(cur["vars"].get(k, 0), float(v))
            else:
                if cur:
                    events.append(cur)
                cur = {"start": ts, "end": ts, "peak": float(row.max()) if len(row) else 0,
                       "vars": {k: float(v) for k, v in top.items()}}
    if cur:
        events.append(cur)
    for e in events:
        e["hours"] = int((e["end"] - e["start"]) / pd.Timedelta(hours=1)) + 1
        e["peak"] = min(e["peak"], 99.0)  # z 分數上限 99（基準幾乎不變的變數 z 會爆大）
        e["vars"] = {k: min(v, 99.0) for k, v in sorted(e["vars"].items(), key=lambda kv: -kv[1])[:3]}
        e["direction"] = {k: ("偏高" if z.loc[e["start"]:e["end"], k].mean() > 0 else "偏低") for k in e["vars"]}
    return events


def detect_anomalies_rolling(clean_df, cols, contamination=0.02, every_days=None):
    """滾動版異常偵測：每次重訓時，只用「到那天為止」的資料建立每小時基準與 IsolationForest，
    套用到下一段（到下次重訓前）的資料。第一次重訓前資料不足，不做偵測（基準建立中）。"""
    from .data import baseline
    every_days = every_days or RETRAIN_DAYS
    cols = [c for c in cols if c in clean_df and clean_df[c].notna().mean() > 0.1]
    idx = clean_df.index
    z_all = pd.DataFrame(np.nan, index=idx, columns=cols)
    score = pd.Series(0.0, index=idx)
    flag = pd.Series(False, index=idx)
    hours = idx.hour
    cuts = cutoffs(idx, every_days)
    first = None
    for i, c in enumerate(cuts):
        tr = clean_df.loc[:c]
        valid_tr = tr[cols].notna().mean(axis=1) >= 0.6 if cols else pd.Series(False, index=tr.index)
        if not cols or valid_tr.sum() < 100:
            continue
        base = baseline(tr, cols, train_frac=1.0)          # 用到這天為止的全部資料建立基準
        use = [k for k in cols if k in base]
        zt = pd.DataFrame({k: (tr[k] - base[k]["median"].reindex(tr.index.hour).values)
                              / base[k]["scale"].reindex(tr.index.hour).values for k in use}, index=tr.index)
        Xt = zt.fillna(0).clip(-20, 20)
        iso = IsolationForest(n_estimators=100, contamination=contamination, random_state=0).fit(Xt[valid_tr])
        thr = float(np.quantile(-iso.score_samples(Xt[valid_tr]), 1 - contamination))
        nxt = cuts[i + 1] if i + 1 < len(cuts) else None
        seg = (idx > c) & ((idx <= nxt) if nxt is not None else True)
        if not seg.any():
            continue
        d = clean_df.loc[seg]
        zs = pd.DataFrame({k: (d[k] - base[k]["median"].reindex(d.index.hour).values)
                              / base[k]["scale"].reindex(d.index.hour).values for k in use}, index=d.index)
        sc = pd.Series(-iso.score_samples(zs.fillna(0).clip(-20, 20)), index=d.index)
        valid = d[cols].notna().mean(axis=1) >= 0.6
        z_all.loc[seg, use] = zs.values
        score[seg] = sc
        flag[seg] = ((sc > thr) | (zs.abs() > 4).any(axis=1)) & valid
        first = first or c
    events = _events(flag, z_all)
    return {"z": z_all, "score": score, "flag": flag, "events": events, "start": first,
            "skipped": None if first is not None else "有效資料不足"}


# ------------------------------------------------------------------ 趨勢預測
def _features(s: pd.Series):
    f = pd.DataFrame(index=s.index)
    for l in (0, 1, 2, 3, 6, 12, 24):
        f[f"lag{l}"] = s.shift(l)
    f["roll6"] = s.rolling(6, min_periods=3).mean()
    f["roll24"] = s.rolling(24, min_periods=12).mean()
    f["d1"] = s.diff()
    h = s.index.hour
    f["hs"], f["hc"] = np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24)
    return f


# 已訓練模型快取：key → 各時距模型與測試表現。訓練一次，之後任何「當下時間」都只做推論。
_FC_CACHE = {}
_MOTOR_CACHE = {}


def forecast(series: pd.Series, horizons=(1, 2, 3, 4, 5, 6), train_frac=0.7, key=None):
    """直接多步預測：每個預測時距訓練一個梯度提升模型（特徵：落後值、滾動平均、時段）。
    以後 30% 為測試集，與「持續法（下一小時=現在）」比較 MAE，證明模型有附加價值。"""
    s = series.astype(float)
    F = _features(s)
    if key is not None and key in _ROLL:
        return _forecast_roll(s, F, key)
    if key is not None and key in _FC_CACHE:
        return _forecast_cached(s, F, _FC_CACHE[key])
    n = int(len(s) * train_frac)
    res = {"horizons": [], "mae_model": [], "mae_naive": [], "pred": [], "lo": [], "hi": []}
    models, qs = [], []
    last = F.iloc[[-1]]
    for h in horizons:
        y = s.shift(-h)
        ok = y.notna() & F["lag0"].notna()
        tr, te = ok & (np.arange(len(s)) < n), ok & (np.arange(len(s)) >= n)
        if tr.sum() < 100 or te.sum() < 24:
            return None
        m = HistGradientBoostingRegressor(**HGB).fit(F[tr], y[tr])
        pte = m.predict(F[te])
        err = y[te] - pte
        res["horizons"].append(h)
        res["mae_model"].append(float(mean_absolute_error(y[te], pte)))
        res["mae_naive"].append(float(mean_absolute_error(y[te], F.loc[te, "lag0"])))
        q = np.nanquantile(np.abs(err), 0.9)
        if key is not None:
            # 快取模式：只用訓練期（前 70%）的模型，回放任何時間點都不會「偷看未來」
            m_use = m
        else:
            m_use = HistGradientBoostingRegressor(**HGB).fit(F[ok], y[ok])
        models.append(m_use)
        qs.append(q)
        p = float(m_use.predict(last)[0]) if last["lag0"].notna().all() else np.nan
        res["pred"].append(p)
        res["lo"].append(p - q)
        res["hi"].append(p + q)
    res["skill"] = 1 - np.mean(res["mae_model"]) / np.mean(res["mae_naive"])  # >0 表示勝過持續法
    res["now"] = float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None
    res["t0"] = s.index[-1]
    res["method"] = "梯度提升模型"
    if res["skill"] <= 0:
        # 模型沒有勝過持續法 → 不採用模型，誠實退回持續法（預測值 = 現值，區間用持續法誤差）
        res["method"] = "持續法（模型未勝過基準，不採用）"
        now = res["now"] if res["now"] is not None else np.nan
        res["pred"] = [now] * len(res["horizons"])
        res["lo"] = [now - m * 1.645 for m in res["mae_naive"]]
        res["hi"] = [now + m * 1.645 for m in res["mae_naive"]]
    if key is not None:
        _FC_CACHE[key] = {"models": models, "q": qs, "horizons": list(res["horizons"]),
                          "mae_model": res["mae_model"], "mae_naive": res["mae_naive"],
                          "skill": res["skill"], "method": res["method"]}
    return res


def _forecast_cached(s, F, c):
    now = float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None
    res = {"horizons": c["horizons"], "mae_model": c["mae_model"], "mae_naive": c["mae_naive"],
           "skill": c["skill"], "method": c["method"], "now": now, "t0": s.index[-1]}
    nv = now if now is not None else np.nan
    if c["skill"] <= 0:
        res["pred"] = [nv] * len(c["horizons"])
        res["lo"] = [nv - m * 1.645 for m in c["mae_naive"]]
        res["hi"] = [nv + m * 1.645 for m in c["mae_naive"]]
        return res
    last = F.iloc[[-1]]
    ok = bool(last["lag0"].notna().all())
    preds = [float(m.predict(last)[0]) if ok else np.nan for m in c["models"]]
    res["pred"] = preds
    res["lo"] = [p - q for p, q in zip(preds, c["q"])]
    res["hi"] = [p + q for p, q in zip(preds, c["q"])]
    return res


# ------------------------------------------------------------------ 滾動重訓（擴展視窗）
# 每 RETRAIN_DAYS 天用「到那天為止」的資料重新訓練一次：前 70% 訓練、後 30% 驗證，
# 沒勝過持續法就不採用。回放到時間 t 時，只用 t 以前最後一次重訓的模型 → 完全不會看到未來。
RETRAIN_DAYS = 7
_ROLL = {}          # key → {"entries": [...], "pred": DataFrame（每小時當時模型的預測）}
_MOTOR_ROLL = {}    # key → [...]


def cutoffs(index, every_days=RETRAIN_DAYS):
    """重訓時間點：資料第一天 0 時起每 every_days 天一次，到資料最後為止。"""
    if len(index) == 0:
        return []
    out, c = [], index[0].normalize() + pd.Timedelta(days=every_days)
    while c <= index[-1]:
        out.append(c)
        c += pd.Timedelta(days=every_days)
    return out


def _next_cutoff(index, t, every_days=RETRAIN_DAYS):
    first = index[0].normalize() + pd.Timedelta(days=every_days)
    if t < first:
        return first
    k = int((t - first) / pd.Timedelta(days=every_days)) + 1
    return first + k * pd.Timedelta(days=every_days)


def _train_forecast(s, horizons, train_frac=0.7):
    """用 s（某次重訓時已有的資料）訓練並驗證；資料不足回傳 None。"""
    F = _features(s)
    n = int(len(s) * train_frac)
    out = {"horizons": [], "models": [], "q": [], "mae_model": [], "mae_naive": []}
    for h in horizons:
        y = s.shift(-h)
        ok = y.notna() & F["lag0"].notna()
        tr, te = ok & (np.arange(len(s)) < n), ok & (np.arange(len(s)) >= n)
        if tr.sum() < 100 or te.sum() < 24:
            return None
        m = HistGradientBoostingRegressor(**HGB).fit(F[tr], y[tr])
        pte = m.predict(F[te])
        out["horizons"].append(h)
        out["models"].append(m)
        out["q"].append(float(np.nanquantile(np.abs(y[te] - pte), 0.9)))
        out["mae_model"].append(float(mean_absolute_error(y[te], pte)))
        out["mae_naive"].append(float(mean_absolute_error(y[te], F.loc[te, "lag0"])))
    out["skill"] = 1 - np.mean(out["mae_model"]) / np.mean(out["mae_naive"])
    out["method"] = "梯度提升模型" if out["skill"] > 0 else "持續法（模型未勝過基準，不採用）"
    if out["skill"] <= 0:
        out["models"] = []          # 不採用就不必保存模型
    return out


def build_forecast_roll(series, key, horizons=(1, 2, 3, 4, 5, 6)):
    """對整段資料依重訓時間點逐次訓練，並算出「每個小時當時的模型」會給的預測（供準度追蹤）。"""
    s = series.astype(float)
    F = _features(s)
    entries = []
    for c in cutoffs(s.index):
        r = _train_forecast(s.loc[:c], horizons)
        if r is None:
            continue
        r.update({"cutoff": c, "hours": int(s.loc[:c].notna().sum()), "n": len(entries) + 1})
        entries.append(r)
    pred = pd.DataFrame({h: F["lag0"] for h in horizons}, index=s.index)   # 尚無模型或不採用 → 持續法
    for i, e in enumerate(entries):
        nxt = entries[i + 1]["cutoff"] if i + 1 < len(entries) else None
        rows = (s.index >= e["cutoff"]) & ((s.index < nxt) if nxt is not None else True) & F["lag0"].notna()
        if e["models"] and rows.any():
            for h, m in zip(e["horizons"], e["models"]):
                pred.loc[rows, h] = m.predict(F[rows])
    _ROLL[key] = {"entries": entries, "pred": pred.astype("float32")}


def _tracking(s, pred, entry, t, days=7):
    """預測準度追蹤：近 days 天內「當時做的預測」與後來真實結果的誤差（只比對已經發生的結果）。"""
    out = []
    for i, h in enumerate(pred.columns):
        y = s.shift(-int(h))
        win = (s.index >= t - pd.Timedelta(days=days)) & (s.index <= t - pd.Timedelta(hours=int(h)))
        e = (pred.loc[win, h] - y[win]).abs().dropna()
        en = (s[win] - y[win]).abs().dropna()
        mae = float(e.mean()) if len(e) else None
        ref = (entry["mae_model"][i] if entry and entry["models"] else entry["mae_naive"][i]) if entry else None
        naive = float(en.mean()) if len(en) else None
        if len(e) < 24:
            status = "樣本不足"
        elif ref and mae > 1.5 * ref:
            status = "誤差變大"           # 比重訓時驗證的誤差大 1.5 倍以上 → 建議提早重訓
        elif naive is not None and mae > naive * 1.05:
            status = "不如持續法"         # 近期實際表現輸給「沿用目前數值」
        else:
            status = "正常"
        out.append({"時距": int(h), "近期誤差": mae, "近期持續法誤差": naive,
                    "訓練時誤差": ref, "樣本": int(len(e)), "狀態": status})
    return out


def _forecast_roll(s, F, key):
    R = _ROLL[key]
    t = s.index[-1]
    usable = [e for e in R["entries"] if e["cutoff"] <= t]
    e = usable[-1] if usable else None
    now = float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None
    nv = now if now is not None else np.nan
    horizons = list(R["pred"].columns)
    res = {"horizons": horizons, "now": now, "t0": t}
    if e is None:
        # 還沒有足夠資料訓練模型 → 持續法，區間用目前已有資料的持續法誤差
        naive = [float((s - s.shift(-h)).abs().mean()) for h in horizons]
        naive = [x if np.isfinite(x) else 0.0 for x in naive]
        res.update({"method": "持續法（資料不足，尚未訓練模型）", "mae_model": naive, "mae_naive": naive, "skill": 0.0,
                    "pred": [nv] * len(horizons), "lo": [nv - 1.645 * m for m in naive],
                    "hi": [nv + 1.645 * m for m in naive]})
    else:
        res.update({"method": e["method"], "mae_model": e["mae_model"], "mae_naive": e["mae_naive"], "skill": e["skill"]})
        last = F.iloc[[-1]]
        if e["models"] and bool(last["lag0"].notna().all()):
            preds = [float(m.predict(last)[0]) for m in e["models"]]
            res.update({"pred": preds, "lo": [p - q for p, q in zip(preds, e["q"])],
                        "hi": [p + q for p, q in zip(preds, e["q"])]})
        else:
            res.update({"pred": [nv] * len(horizons), "lo": [nv - 1.645 * m for m in e["mae_naive"]],
                        "hi": [nv + 1.645 * m for m in e["mae_naive"]]})
    res["version"] = {"重訓次數": e["n"] if e else 0, "重訓時間": e["cutoff"] if e else None,
                      "訓練資料時數": e["hours"] if e else int(s.notna().sum()),
                      "下次重訓": _next_cutoff(s.index, t),   # 依排程推算，不從未來的紀錄查
                      "每幾天重訓": RETRAIN_DAYS}
    res["tracking"] = _tracking(s, R["pred"].loc[:t], e, t)
    return res


def time_to_threshold(fc, cut, lower_is_bad=False):
    """預測序列第一次越過門檻的時距（小時）；None 表示 6 小時內不會越過。"""
    if not fc:
        return None
    for h, p in zip(fc["horizons"], fc["pred"]):
        if (p < cut) if lower_is_bad else (p >= cut):
            return h
    return None


# ------------------------------------------------------------------ 設備故障機率
def _motor_xy(df: pd.DataFrame, horizon=24):
    online = df["power"].fillna(0) > 5  # 功率 0 = 整台主機斷線，不算馬達停機
    cur = df["current"].where(online)
    stop = (cur < 0.5).astype(float).where(cur.notna())
    X = pd.DataFrame(index=df.index)
    X["cur_mean24"] = cur.rolling(24, min_periods=6).mean()
    X["cur_std24"] = cur.rolling(24, min_periods=6).std()
    X["cur_trend"] = cur.rolling(6, min_periods=3).mean() - cur.rolling(48, min_periods=12).mean()
    X["pf_mean24"] = df["pf"].rolling(24, min_periods=6).mean()
    X["stops72"] = stop.rolling(72, min_periods=1).sum()
    grp = (stop == 1).cumsum()
    X["h_since_stop"] = stop.groupby(grp).cumcount().clip(upper=500)
    X["wtemp"] = df.get("wtemp")
    X["level"] = df.get("level")
    X["hour"] = df.index.hour
    y = stop[::-1].rolling(horizon, min_periods=1).max()[::-1].shift(-1)
    # 最後 horizon 小時的答案（未來 24h 有沒有停機）還不完整，不能拿來訓練或驗證
    ok = y.notna() & X["cur_mean24"].notna() & (np.arange(len(df)) < len(df) - horizon)
    return X, y, ok, cur, stop


def _train_motor(df, horizon=24, train_frac=0.7):
    X, y, ok, _, _ = _motor_xy(df, horizon)
    n = int(len(df) * train_frac)
    idx = np.arange(len(df))
    tr, te = ok & (idx < n), ok & (idx >= n)
    if tr.sum() < 100 or y[tr].nunique() < 2:
        return None
    clf = HistGradientBoostingClassifier(**HGB).fit(X[tr], y[tr])
    auc = float(roc_auc_score(y[te], clf.predict_proba(X[te])[:, 1])) if te.sum() and y[te].nunique() > 1 else None
    return {"clf": clf, "auc": auc, "base_rate": float(y[ok].mean())}


def build_motor_roll(df, key, horizon=24):
    """馬達停機模型的滾動重訓：每次只用到重訓時間點為止的資料。"""
    entries = []
    for c in cutoffs(df.index):
        r = _train_motor(df.loc[:c], horizon)
        if r is None:
            continue
        r.update({"cutoff": c, "n": len(entries) + 1})
        entries.append(r)
    _MOTOR_ROLL[key] = entries


def motor_stop_model(df: pd.DataFrame, horizon=24, train_frac=0.7, key=None):
    """預測 CPM 馬達「未來 24 小時內發生停機（電流 < 0.5A）」的機率。"""
    X, y, ok, cur, stop = _motor_xy(df, horizon)
    version = None
    if key is not None and key in _MOTOR_ROLL:
        usable = [e for e in _MOTOR_ROLL[key] if e["cutoff"] <= df.index[-1]]
        if usable:
            e = usable[-1]
            clf_use, auc, base_rate = e["clf"], e["auc"], e["base_rate"]
            version = {"重訓次數": e["n"], "重訓時間": e["cutoff"]}
        else:
            # 尚無模型：以目前已有資料的停機頻率估計
            clf_use, auc = None, None
            base_rate = float(y[ok].mean()) if ok.any() else 0.0
            version = {"重訓次數": 0, "重訓時間": None}
    elif key is not None and key in _MOTOR_CACHE:
        c = _MOTOR_CACHE[key]
        clf_use, auc, base_rate = c["clf"], c["auc"], c["base_rate"]
    else:
        r = _train_motor(df, horizon, train_frac)
        clf_use, auc, base_rate = (r["clf"], r["auc"], r["base_rate"]) if r else (None, None, float(y[ok].mean()))
        if key is not None and r:
            _MOTOR_CACHE[key] = r
    p_now = float(clf_use.predict_proba(X.iloc[[-1]])[:, 1][0]) if clf_use is not None else base_rate
    stop_hours = df.index[stop == 1]
    by_hour = pd.Series(stop_hours.hour).value_counts().sort_index()
    return {"p_now": p_now, "auc": auc, "base_rate": base_rate, "n_stop_hours": int((stop == 1).sum()),
            "peak_hours": by_hour.sort_values(ascending=False).head(3).index.tolist(),
            "cur_mean7d": float(cur.iloc[-168:].mean()), "cur_mean_all": float(cur.mean()),
            "method": "停機分類模型" if clf_use is not None else "歷史停機頻率（資料不足，尚未訓練模型）",
            "version": version}


def linear_eta(series: pd.Series, floor: float, days=14):
    """以近 N 天日均值的線性趨勢，估計多少天後降到 floor（例：電池、探棒讀值）。"""
    d = series.resample("D").mean().dropna().iloc[-days:]
    if len(d) < 5:
        return None, None
    x = np.arange(len(d))
    slope, icpt = np.polyfit(x, d.values, 1)
    now = d.values[-1]
    if slope >= 0:
        return None, float(slope)
    return max(0.0, float((floor - now) / slope)), float(slope)
