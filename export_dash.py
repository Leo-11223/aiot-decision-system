"""把決策閉環的結果整理成儀表板用的資料表（outputs/dash/*.csv）。用法：python export_dash.py"""
import collections
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
import run
from aiot.data import load_all

OUT = Path(__file__).resolve().parent / "outputs" / "dash"
OUT.mkdir(parents=True, exist_ok=True)

LABEL = {"thi": "成羊區THI", "thi_lamb": "仔羊區THI", "temp": "溫度", "rh": "濕度", "co2": "CO2", "nh3": "NH3",
         "temp_avg": "平均溫度", "nh3_avg": "成羊區平均NH3", "pm25": "PM2.5", "pm10": "PM10", "co": "CO",
         "hcho": "HCHO", "tvoc": "TVOC", "noise": "噪音", "wind": "風速", "ph": "pH", "ec": "EC", "do": "DO",
         "wtemp": "水溫", "battery": "電池電量", "batt_v": "電池電壓", "level": "水位", "current": "馬達電流",
         "power": "馬達功率", "pf": "功率因數", "heat_index": "熱指數"}


def lab(k):
    for p in ("do", "ph", "orp", "sc"):
        if k.startswith(p) and k[len(p):].isdigit():
            return p.upper().replace("PH", "pH") + k[len(p):]
    return LABEL.get(k, k)


def main():
    S = run.main()
    sites = load_all()
    W = lambda name, df: df.to_csv(OUT / name, index=False, encoding="utf-8")

    # 1 場域狀態
    rows = []
    for sid, r in S["sites"].items():
        st, an = r["status"], r["anomaly"]
        rows.append({"site_id": sid, "場域": r["name"], "領域": r["domain"], "指數": st["指數"], "數值": st["數值"],
                     "等級": st["等級"], "等級碼": st["等級碼"], "感測可信度": st["感測可信度"],
                     "異常比例": round(an["異常小時數"] / an["總小時數"], 4), "異常事件": an["事件數"],
                     "資料期間": f"{r['period'][0][:10]} ~ {r['period'][1][:10]}", "最新時間": st["時間"][:16],
                     "決策數": len(r["decisions"]), "工單數": len(r["work_orders"])})
    W("site_status.csv", pd.DataFrame(rows))

    # 2 標準決策資料
    dec = pd.DataFrame(S["decisions"])
    dec.insert(0, "decision_id", [f"D{i+1:02d}" for i in range(len(dec))])
    dec["成效已驗證"] = dec["成效已驗證"].map({True: "是", False: "否"})
    dec["人工確認"] = dec["人工確認"].map({True: "需要", False: "不需要"})
    W("decisions.csv", dec)

    # 3 工單（管理優先）
    wo = pd.DataFrame(S["work_orders"])
    wo.insert(0, "wo_id", [f"W{i+1:02d}" for i in range(len(wo))])
    W("work_orders.csv", wo)

    # 4 異常診斷
    dg = [{"場域": r["name"], **d} for r in S["sites"].values() for d in r["diagnoses"]]
    dg = pd.DataFrame(dg)
    dg.insert(0, "diag_id", [f"G{i+1:02d}" for i in range(len(dg))])
    W("diagnoses.csv", dg)

    # 5 預測模型表現
    fr = []
    for sid, r in S["sites"].items():
        for f in r["forecast"]:
            if f:
                fr.append({"model_id": f"{sid}-{f['變數']}", "場域": r["name"], "變數": f["變數"],
                           "方法": f["方法"], "模型MAE_1h": f["模型MAE"][0], "持續法MAE_1h": f["持續法MAE"][0],
                           "模型MAE_6h": f["模型MAE"][-1], "持續法MAE_6h": f["持續法MAE"][-1],
                           "改善率": f["改善率"] / 100, "現值": f["現值"], "6h預測": f["預測"][-1], "單位": f["單位"]})
    W("forecast_skill.csv", pd.DataFrame(fr))

    # 6 羊舍 THI：近 72 小時實測＋未來 6 小時預測
    sh = S["series"]["sheep_hourly"]
    t = pd.to_datetime(sh["t"])[-72:]
    act = pd.DataFrame({"ts": t.strftime("%Y-%m-%d %H:00"), "kind": "實測", "thi": sh["thi"][-72:],
                        "lo": None, "hi": None})
    f = S["sites"]["sheep"]["forecast"][0]
    ft = pd.date_range(t[-1], periods=7, freq="h")[1:]
    fc = pd.DataFrame({"ts": ft.strftime("%Y-%m-%d %H:00"), "kind": "預測", "thi": f["預測"], "lo": f["下界"], "hi": f["上界"]})
    W("thi_forecast.csv", pd.concat([act, fc]))

    # 7 羊舍閉環回放（每日）
    sr = pd.DataFrame({"ts": pd.to_datetime(sh["t"]), "thi": sh["thi"], "rec": sh["rec"], "act": sh["act"]}).set_index("ts")
    dd = sr.resample("D").agg({"thi": "max"})
    dd["建議開風扇時數"] = sr["rec"].gt(0).resample("D").sum()
    dd["實際開風扇時數"] = sr["act"].gt(0).resample("D").sum()
    dd["THI危險時數"] = sr["thi"].ge(79).resample("D").sum()
    dd = dd.rename(columns={"thi": "THI最高"}).reset_index()
    dd["ts"] = dd["ts"].dt.strftime("%Y-%m-%d")
    W("sheep_replay_daily.csv", dd)

    # 8 動作成效驗證
    ef = [{"動作": e["action"], "對象": "羊舍 THI", "效果": e["effect"], "標準誤": e.get("se"), "樣本數": e["n"],
           "已驗證": "是" if e["verified"] else "否"} for e in S["feedback"]["sheep_effects"]]
    me = S["feedback"]["motor_effect"]
    ef.append({"動作": "CPM 馬達停機", "對象": "水產池 DO（同時段配對）", "效果": me["level_gap_same_hour"],
               "標準誤": None, "樣本數": me["n"], "已驗證": "是" if me["verified"] else "否"})
    W("action_effects.csv", pd.DataFrame(ef))

    # 9 感測器可信度
    tr = []
    for sid, s in sites.items():
        for k in s["trust"].columns:
            reasons = collections.Counter(w for v in s["reason"][k] for w in v.split(";") if w)
            top = "、".join(f"{w}({n}h)" for w, n in reasons.most_common(2)) or "—"
            tr.append({"sensor_id": f"{sid}:{k}", "場域": s["cfg"]["name"], "感測器": lab(k),
                       "全期可信度": round(float(s["trust"][k].mean()), 1),
                       "近6h可信度": round(float(s["trust"][k].iloc[-6:].mean()), 1), "主要扣分原因": top})
    W("sensor_trust.csv", pd.DataFrame(tr).sort_values("近6h可信度"))

    # 10 專家系統 DO 三探棒（日中位數）
    ex = S["series"]["expert_daily"]
    W("expert_do_daily.csv", pd.DataFrame({"day": ex["t"], "DO1": ex["do1"], "DO2": ex["do2"], "DO3": ex["do3"]}))
    co = S["series"]["co_daily"]
    W("co_daily.csv", pd.DataFrame({"day": co["t"], "育成一廠": co["breed1"], "東北角": co["northeast"]}))
    rv = S["series"]["river_daily"]
    W("river_daily.csv", pd.DataFrame({"day": rv["t"], "DO": rv["do"], "電池最低": rv["battery"]}))
    print("exported", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
