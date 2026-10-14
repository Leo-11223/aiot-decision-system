"""03 AI 分析：狀態辨識（指數＋分級）、異常偵測、趨勢預測、設備故障機率。"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import mean_absolute_error, roc_auc_score

LEVEL_NAME = ["正常", "警戒", "危險", "緊急"]


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


_AQI_PM25 = [(0, 15.4, 0, 50), (15.5, 35.4, 51, 100), (35.5, 54.4, 101, 150),
             (54.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500)]


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
    # 合併連續異常小時為事件
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
    return {"z": z, "score": score, "flag": flag, "threshold": thr, "events": events}


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
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, random_state=0).fit(F[tr], y[tr])
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
            m_use = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, random_state=0).fit(F[ok], y[ok])
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


def time_to_threshold(fc, cut, lower_is_bad=False):
    """預測序列第一次越過門檻的時距（小時）；None 表示 6 小時內不會越過。"""
    if not fc:
        return None
    for h, p in zip(fc["horizons"], fc["pred"]):
        if (p < cut) if lower_is_bad else (p >= cut):
            return h
    return None


# ------------------------------------------------------------------ 設備故障機率
def motor_stop_model(df: pd.DataFrame, horizon=24, train_frac=0.7, key=None):
    """預測 CPM 馬達「未來 24 小時內發生停機（電流 < 1A）」的機率。"""
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
    ok = y.notna() & X["cur_mean24"].notna()
    if key is not None and key in _MOTOR_CACHE:
        c = _MOTOR_CACHE[key]
        clf_use, auc, base_rate = c["clf"], c["auc"], c["base_rate"]
    else:
        n = int(len(df) * train_frac)
        idx = np.arange(len(df))
        tr, te = ok & (idx < n), ok & (idx >= n)
        clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, random_state=0).fit(X[tr], y[tr])
        auc = float(roc_auc_score(y[te], clf.predict_proba(X[te])[:, 1])) if y[te].nunique() > 1 else None
        base_rate = float(y[ok].mean())
        if key is not None:
            clf_use = clf  # 快取模式只用訓練期模型
            _MOTOR_CACHE[key] = {"clf": clf, "auc": auc, "base_rate": base_rate}
        else:
            clf_use = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, random_state=0).fit(X[ok], y[ok])
    p_now = float(clf_use.predict_proba(X.iloc[[-1]])[:, 1][0])
    stop_hours = df.index[stop == 1]
    by_hour = pd.Series(stop_hours.hour).value_counts().sort_index()
    return {"p_now": p_now, "auc": auc, "base_rate": base_rate, "n_stop_hours": int((stop == 1).sum()),
            "peak_hours": by_hour.sort_values(ascending=False).head(3).index.tolist(),
            "cur_mean7d": float(cur.iloc[-168:].mean()), "cur_mean_all": float(cur.mean())}


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
