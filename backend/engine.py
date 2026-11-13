"""決策引擎服務層：啟動時載入資料、訓練模型一次；之後可對「任一時間點」快速產生決策快照。

時間切片的原則：只用該時間點（含）以前的資料；預測、停機、異常偵測模型每週以「到那天為止」的資料重訓，
回放到 t 時只用 t 以前最後一次重訓的模型，所以完全不會偷看未來。
"""
import hashlib
import threading
import time
from collections import OrderedDict

import numpy as np
import pandas as pd

from aiot import analysis as A
from aiot import explain as X
from aiot import decision as D
from aiot import feedback as F
from aiot.config import SITES
from aiot.data import load_all


# 要做滾動重訓的預測：key → (場域, 欄位)；key 與 decision.py 呼叫 A.forecast 時相同
FORECAST_KEYS = {"sheep:thi": ("sheep", "thi"), "expert:do1": ("expert", "do1"), "expert:current": ("expert", "current"),
                 "river:do": ("river", "do"), "river:ec": ("river", "ec"), "breed1:pm25": ("breed1", "pm25"),
                 "northeast:pm25": ("northeast", "pm25"),
                 "site:pm10": ("site", "pm10")}


def jsonable(o):
    """把 numpy／pandas 型別轉成可輸出 JSON 的 Python 型別。"""
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o is None or np.isnan(o) or np.isinf(o)) else round(float(o), 4)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return o.strftime("%Y-%m-%dT%H:%M")
    return o


def short_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


class Engine:
    def __init__(self, cache_size=256):
        t0 = time.time()
        step = lambda msg: print(f"  [{time.time() - t0:5.0f} 秒] {msg}", flush=True)
        self.sites = load_all()
        step("資料載入與清洗完成")
        self.verify = F.verify_sheep(self.sites["sheep"])          # 寫入成效資料庫
        self.motor_effect = F.verify_expert(self.sites["expert"])
        # 異常偵測：每週以「到那天為止」的資料重建基準與模型，只套用到之後的資料
        self.anomalies = {sid: A.detect_anomalies_rolling(s["clean"], s["cfg"]["anomaly"])
                          for sid, s in self.sites.items()}
        step("成效驗證與滾動異常偵測完成")
        # 預測與停機模型：每週擴展視窗重訓（見 analysis.build_forecast_roll）
        for key, (sid, col) in FORECAST_KEYS.items():
            if sid in self.sites and col in self.sites[sid]["clean"]:
                A.build_forecast_roll(self.sites[sid]["clean"][col], key)
        A.build_motor_roll(self.sites["expert"]["df"], "expert:motor")
        step(f"滾動重訓完成（每 {A.RETRAIN_DAYS} 天一次）")
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self.decision_index = {}   # decision_id → 決策內容（供核准／駁回時存檔）
        # 暖機：以各場域最後時間跑一次，訓練並快取所有模型
        for sid in self.sites:
            self.snapshot(sid)
            self.timeline(sid)
            step(f"{sid} 模型訓練完成")

    # ------------------------------------------------------------ 預先訓練的模型存檔（部署用）
    # 部署時先在打包階段訓練好並存檔，雲端主機啟動時只需讀檔，不必重新訓練。
    def __getstate__(self):
        st = dict(self.__dict__)
        st.pop("_lock", None)  # 執行緒鎖無法存檔
        st["_model_cache"] = (A._FC_CACHE, A._MOTOR_CACHE, A._ROLL, A._MOTOR_ROLL)  # 已訓練模型放在模組層級，一起存
        return st

    def __setstate__(self, st):
        fc, motor, roll, mroll = st.pop("_model_cache", ({}, {}, {}, {}))
        A._FC_CACHE.update(fc)
        A._MOTOR_CACHE.update(motor)
        A._ROLL.update(roll)
        A._MOTOR_ROLL.update(mroll)
        self.__dict__.update(st)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 時間
    def time_range(self, sid):
        idx = self.sites[sid]["df"].index
        return idx[0], idx[-1]

    def resolve_time(self, sid, at=None):
        """把使用者給的時間對齊到整點並限制在資料範圍內；沒給就用最後一筆。"""
        lo, hi = self.time_range(sid)
        if at is None:
            return hi
        t = pd.Timestamp(at).floor("h")
        if t.tzinfo is not None:
            t = t.tz_localize(None)
        min_t = lo + pd.Timedelta(hours=72)   # 至少要有 3 天歷史才能算滾動特徵
        return min(max(t, min_t), hi)

    def _slice(self, sid, t):
        s = self.sites[sid]
        out = dict(s)
        for k in ("df", "raw", "clean", "trust", "reason"):
            out[k] = s[k].loc[:t]
        return out

    # ------------------------------------------------------------ 快照
    def snapshot(self, sid, at=None):
        t = self.resolve_time(sid, at)
        key = (sid, t)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        site = self._slice(sid, t)
        ctx = {"sites": self.sites, "verify": self.verify, "motor_effect": self.motor_effect,
               "replay": F.replay_sheep(site, D.sheep_rule) if sid == "sheep" else None}
        r = D.BUILDERS[sid](site, ctx)
        an_full = self.anomalies[sid]
        an = {"flag": an_full["flag"].loc[:t], "events": self._events_until(an_full, t)}
        r["anomaly"] = D._anom_summary(site, an)
        r["trust_now"] = {k: round(float(v), 1) for k, v in site["trust"].iloc[-6:].mean().items()}
        r["site_id"], r["name"], r["domain"] = sid, site["cfg"]["name"], site["cfg"]["domain"]
        r["at"] = t
        lo, hi = self.time_range(sid)
        r["range"] = {"start": lo, "end": hi}
        # 決策與工單加上穩定 id
        for d in r["decisions"]:
            d["id"] = short_id(sid, d["設備"], d["動作"], t)
            d["site_id"] = sid
            self.decision_index[d["id"]] = {"site_id": sid, "at": t, "decision": d}
        for w in r["work_orders"]:
            w["id"] = short_id(sid, w["工單"])
            w["site_id"] = sid
        r = jsonable(r)
        # 白話說明（命題的三個問題）與每個決策的一句話
        r["白話"] = X.site_summary(r, site["cfg"], self.INDICATOR[sid])
        for d in r["decisions"]:
            d["一句話"] = X.decision_line(d)
        with self._lock:
            self._cache[key] = r
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return r

    @staticmethod
    def _events_until(an_full, t):
        """t 以前的異常事件；跨過 t 的進行中事件只算到 t 為止（只用 t 以前的資料重算）。"""
        out = []
        for e in an_full["events"]:
            if e["end"] <= t:
                out.append(e)
            elif e["start"] <= t:
                out += A._events(an_full["flag"].loc[e["start"]:t], an_full["z"].loc[e["start"]:t])
        return out

    # ------------------------------------------------------------ 時間序列
    def series(self, sid, cols, at=None, hours=72, step=1):
        """取「當下時間」往前 hours 小時的資料；step>1 時每 step 小時彙整成平均、最小、最大值。"""
        t = self.resolve_time(sid, at)
        s = self.sites[sid]
        start = t - pd.Timedelta(hours=hours - 1)
        cols = [c for c in cols if c in s["df"].columns]
        df = s["df"].loc[start:t, cols]
        trust = s["trust"].loc[start:t, [c for c in cols if c in s["trust"].columns]]
        out = {"site_id": sid, "at": t, "step": step, "thresholds": SITES[sid].get("thresholds", {})}
        if step > 1:
            rule = f"{step}h"
            g = df.resample(rule, origin=start)
            mean, lo, hi = g.mean(), g.min(), g.max()
            tg = trust.resample(rule, origin=start).mean()
            out.update({"t": list(mean.index), "values": {c: mean[c].tolist() for c in cols},
                        "min": {c: lo[c].tolist() for c in cols}, "max": {c: hi[c].tolist() for c in cols},
                        "trust": {c: tg[c].tolist() for c in tg.columns}})
        else:
            out.update({"t": list(df.index), "values": {c: df[c].tolist() for c in cols},
                        "trust": {c: trust[c].tolist() for c in trust.columns}})
        return jsonable(out)

    # ------------------------------------------------------------ 時間軸事件標記（拉條上的點）
    INDICATOR = {
        "sheep": ("成羊區 THI", "", SITES["sheep"]["thresholds"]["thi"], False),
        "expert": ("DO 共識值", "mg/L", SITES["expert"]["thresholds"]["do"], True),
        "river": ("DO（48h 中位數）", "mg/L", SITES["river"]["thresholds"]["do"], True),
        "breed1": ("PM2.5（24h 平均）", "µg/m³", SITES["breed1"]["thresholds"]["pm25"], False),
        "breed1_north": ("PM2.5（24h 平均）", "µg/m³", SITES["breed1_north"]["thresholds"]["pm25"], False),
        "northeast": ("PM2.5（24h 平均）", "µg/m³", SITES["northeast"]["thresholds"]["pm25"], False),
        "site": ("PM10", "µg/m³", SITES["site"]["thresholds"]["pm10"], False),
    }

    def indicator(self, sid):
        """每小時的主指標（與快照中的等級判斷一致），只用該時間點以前的資料。"""
        s = self.sites[sid]
        c, t = s["clean"], s["trust"]
        if sid == "sheep":
            return c["thi"]
        if sid == "expert":
            cols = s["cfg"]["groups"]["DO"]
            ok = c[cols].where(t[cols] >= 80)
            return ok.median(axis=1)
        if sid == "river":
            return s["df"]["do"].rolling(48, min_periods=12).median()
        if sid == "site":
            return c["pm10"]
        return c["pm25"].rolling(24, min_periods=6).mean()

    def timeline(self, sid):
        key = ("timeline", sid)
        if key in self._cache:
            return self._cache[key]
        s = self.sites[sid]
        label, unit, cuts, low_bad = self.INDICATOR[sid]
        v = self.indicator(sid)
        lvl = v.apply(lambda x: -1 if pd.isna(x) else A.level_of(x, cuts, lower_is_bad=low_bad))
        events = []

        def runs(mask):
            out, start = [], None
            for ts, m in mask.items():
                if m and start is None:
                    start = ts
                if not m and start is not None:
                    out.append((start, prev))
                    start = None
                prev = ts
            if start is not None:
                out.append((start, prev))
            return out

        for a, b in runs(lvl >= 2):
            peak = int(lvl.loc[a:b].max())
            events.append({"type": A.LEVEL_NAME[peak], "start": a, "end": b,
                           "hours": int((b - a) / pd.Timedelta(hours=1)) + 1})
        for e in self.anomalies[sid]["events"]:
            if e["hours"] >= 3:
                events.append({"type": "異常", "start": e["start"], "end": e["end"], "hours": e["hours"],
                               "detail": "、".join(e["vars"])})
        # 長期失效的感測器（全期平均可信度 <50）已有專門診斷，不在時間軸上重複標記
        tr = s["trust"][[k for k in s["trust"].columns if s["trust"][k].mean() >= 50]]
        low = (tr.min(axis=1) < 50) & s["raw"].notna().any(axis=1)
        for a, b in runs(low):
            h = int((b - a) / pd.Timedelta(hours=1)) + 1
            if h >= 3:
                bad = tr.loc[a:b].mean()
                events.append({"type": "感測器", "start": a, "end": b, "hours": h,
                               "detail": "、".join(bad[bad < 50].index[:3])})
        events.sort(key=lambda e: e["start"])
        r = jsonable({"site_id": sid, "start": v.index[0], "end": v.index[-1], "label": label, "unit": unit,
                      "cuts": cuts, "lower_is_bad": low_bad,
                      "value": v.round(2).tolist(), "level": lvl.astype(int).tolist(), "events": events})
        self._cache[key] = r
        return r

    def columns(self, sid):
        s = self.sites[sid]
        return {"key": s["cfg"]["key"], "all": [c for c in s["df"].columns]}
