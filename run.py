"""一鍵執行完整決策閉環：

  01 IoT 感測  → data/*.csv（平台匯出）
  02 大數據    → 清洗、感測器可信度、歷史基準         aiot/data.py
  03 AI 分析   → 狀態辨識、異常偵測、趨勢預測、診斷   aiot/analysis.py
  04 決策引擎  → 標準決策資料＋安全分級＋管理工單     aiot/decision.py
  05 執行回饋  → 動作成效驗證、成效資料庫、閉環回放   aiot/feedback.py

輸出：outputs/decisions.json、decisions.csv、work_orders.csv、dashboard_data.json
用法：python run.py
"""
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from aiot import analysis as A
from aiot import decision as D
from aiot import feedback as F
from aiot.data import load_all

OUT = Path(__file__).resolve().parent / "outputs"
OUT.mkdir(exist_ok=True)


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o is None or np.isnan(o)) else round(float(o), 4)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o)
    return o


def main():
    print("[1/4] 讀取資料、計算感測器可信度…", flush=True)
    sites = load_all()                                             # 01 + 02
    print("[2/4] 驗證動作成效（執行回饋）…", flush=True)
    verify = F.verify_sheep(sites["sheep"])                        # 05 → 寫入成效資料庫
    replay = F.replay_sheep(sites["sheep"], D.sheep_rule)
    motor_eff = F.verify_expert(sites["expert"])
    ctx = {"sites": sites, "verify": verify, "replay": replay, "motor_effect": motor_eff}

    results = {}
    print("[3/4] AI 分析與決策（每個場域約 10–40 秒）…", flush=True)
    for sid, s in sites.items():
        print(f"      → {s['cfg']['name']}", flush=True)
        cfg = s["cfg"]
        an = A.detect_anomalies(s["clean"], cfg["anomaly"], s["baseline"])   # 03
        r = D.BUILDERS[sid](s, ctx)                                          # 04
        r["anomaly"] = D._anom_summary(s, an)
        r["trust"] = {k: round(float(v), 1) for k, v in s["trust"].mean().items()}
        r["trust_now"] = {k: round(float(v), 1) for k, v in s["trust"].iloc[-6:].mean().items()}
        r["period"] = [str(s["df"].index[0]), str(s["df"].index[-1])]
        r["name"], r["domain"], r["devices"] = cfg["name"], cfg["domain"], cfg["devices"]
        r["_an"] = an
        results[sid] = r

    print("[4/4] 輸出結果檔…", flush=True)
    decisions = [d for r in results.values() for d in r["decisions"]]
    wos = sorted([w for r in results.values() for w in r["work_orders"]], key=lambda w: -w["優先分數"])
    pd.DataFrame(decisions).to_csv(OUT / "decisions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(wos).to_csv(OUT / "work_orders.csv", index=False, encoding="utf-8-sig")

    # ---------------- 儀表板資料（時間序列降採樣）
    series = {}
    sh = replay["series"]
    series["sheep_hourly"] = {"t": [str(t) for t in sh.index], "thi": sh["thi"].round(2).tolist(),
                              "rec": sh["rec_fans"].tolist(), "act": sh["act_fans"].tolist()}
    e = sites["expert"]
    ed = e["df"][["do1", "do2", "do3", "sc1", "sc2", "sc3", "current"]].resample("D").median()
    series["expert_daily"] = {"t": [str(t.date()) for t in ed.index], **{k: ed[k].round(3).tolist() for k in ed}}
    rv = sites["river"]["df"][["do", "ec", "battery"]].resample("D").agg({"do": "median", "ec": "median", "battery": "min"})
    series["river_daily"] = {"t": [str(t.date()) for t in rv.index], **{k: rv[k].round(3).tolist() for k in rv}}
    air = pd.concat({k: sites[k]["clean"]["pm25"].resample("D").mean() for k in ["breed1", "breed1_north", "northeast"]}, axis=1)
    series["air_daily"] = {"t": [str(t.date()) for t in air.index], **{k: air[k].round(2).tolist() for k in air}}
    co = pd.concat({k: sites[k]["df"]["co"].resample("D").median() for k in ["breed1", "northeast"]}, axis=1)
    series["co_daily"] = {"t": [str(t.date()) for t in co.index], **{k: co[k].round(2).tolist() for k in co}}
    st = sites["site"]["df"][["pm10", "noise"]].resample("D").agg({"pm10": "max", "noise": "max"})
    series["site_daily"] = {"t": [str(t.date()) for t in st.index], **{k: st[k].round(1).tolist() for k in st}}

    summary = {
        "sites": {sid: {k: v for k, v in r.items() if k != "_an"} for sid, r in results.items()},
        "decisions": decisions, "work_orders": wos,
        "feedback": {"sheep_effects": verify["effects"], "sheep_replay": {k: v for k, v in replay.items() if k not in ("daily", "series")},
                     "motor_effect": motor_eff},
        "series": series,
        "mode_counts": pd.Series([d["執行模式"] for d in decisions]).value_counts().to_dict(),
    }
    summary = _clean(summary)
    (OUT / "dashboard_data.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "decisions.json").write_text(json.dumps(_clean(decisions), ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 主控台摘要
    print(f"{'場域':<24}{'狀態':<8}{'指數':>8}  決策數  工單數")
    for sid, r in results.items():
        st = r["status"]
        print(f"{r['name']:<22}{st['等級']:<8}{str(st['數值']):>8}  {len(r['decisions']):>4}  {len(r['work_orders']):>4}")
    print("執行模式：", summary["mode_counts"])
    return summary


if __name__ == "__main__":
    main()
