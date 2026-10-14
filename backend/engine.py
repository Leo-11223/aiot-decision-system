"""決策引擎服務層：啟動時載入資料、訓練模型一次；之後可對「任一時間點」快速產生決策快照。

時間切片的原則：只用該時間點（含）以前的資料，模型只用訓練期資料，所以回放時不會偷看未來。
"""
import hashlib
import threading
from collections import OrderedDict

import numpy as np
import pandas as pd

from aiot import analysis as A
from aiot import decision as D
from aiot import feedback as F
from aiot.config import SITES
from aiot.data import load_all


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
        self.sites = load_all()
        self.verify = F.verify_sheep(self.sites["sheep"])          # 寫入成效資料庫
        self.motor_effect = F.verify_expert(self.sites["expert"])
        self.anomalies = {sid: A.detect_anomalies(s["clean"], s["cfg"]["anomaly"], s["baseline"])
                          for sid, s in self.sites.items()}
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self.decision_index = {}   # decision_id → 決策內容（供核准／駁回時存檔）
        # 暖機：以各場域最後時間跑一次，訓練並快取所有模型
        for sid in self.sites:
            self.snapshot(sid)

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
        an = {"flag": an_full["flag"].loc[:t], "events": [e for e in an_full["events"] if e["end"] <= t]}
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
        with self._lock:
            self._cache[key] = r
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return r

    # ------------------------------------------------------------ 時間序列
    def series(self, sid, cols, at=None, hours=72):
        t = self.resolve_time(sid, at)
        s = self.sites[sid]
        start = t - pd.Timedelta(hours=hours - 1)
        cols = [c for c in cols if c in s["df"].columns]
        df = s["df"].loc[start:t, cols]
        trust = s["trust"].loc[start:t, [c for c in cols if c in s["trust"].columns]]
        return jsonable({
            "site_id": sid, "at": t, "t": [x for x in df.index],
            "values": {c: df[c].tolist() for c in cols},
            "trust": {c: trust[c].tolist() for c in trust.columns},
            "thresholds": SITES[sid].get("thresholds", {}),
        })

    def columns(self, sid):
        s = self.sites[sid]
        return {"key": s["cfg"]["key"], "all": [c for c in s["df"].columns]}
