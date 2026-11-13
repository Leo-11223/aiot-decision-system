"""04 決策引擎：目標、規則、風險、成本 → 標準決策資料
(設備／動作／時間／幅度／理由／預期效果／風險／人工確認／Fallback)。

安全控制原則（AI 提出決策；SCADA 呈現、告警與管理流程；PLC 保留安全連鎖與緊急控制）：
  · 低風險 且 已驗證 且 感測器可信（≥80） → 自動執行
  · 高／中風險，或成效尚未驗證            → 人工確認
  · 關鍵感測器不可信（<50）               → Fallback（備援量測／保守動作／派工）
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis as A
from . import diagnose as G
from . import feedback as F
from .config import Z_ONE_SIDED, MIN_SAMPLES, TRUST_AUTO, TRUST_FALLBACK, AIR_PEERS
from .data import thi_formula
from .feedback import EFFECT_DB

# ---- 成本假設（現場可替換）：風扇單台 0.75 kW、電價 NT$4/kWh
FAN_KW, PRICE = 0.75, 4.0


def gate(risk: str, trust: float, verified: bool):
    if trust is None or trust < TRUST_FALLBACK:
        return "Fallback", True
    if risk == "低" and verified and trust >= TRUST_AUTO:
        return "自動執行", False
    return "人工確認", True


def decision(site, device, action, when, magnitude, reason, effect, risk, trust, verified, fallback,
             cost="—", priority=2):
    mode, confirm = gate(risk, trust, verified)
    return {"場域": site, "設備": device, "動作": action, "時間": when, "幅度": magnitude, "理由": reason,
            "預期效果": effect, "風險": risk, "感測可信度": None if trust is None else round(float(trust)),
            "成效已驗證": verified, "執行模式": mode, "人工確認": confirm, "Fallback": fallback,
            "成本": cost, "優先": priority}


def work_order(site, title, kind, detail, severity, parts="—", due="7 天內"):
    # 管理優先分數：嚴重度 × 類別權重（安全 > 生產 > 數據品質 > 能耗）
    w = {"安全": 4, "生產": 3, "數據品質": 2, "能耗": 1}[kind]
    return {"場域": site, "工單": title, "類別": kind, "說明": detail, "嚴重度": severity,
            "優先分數": severity * w, "備品": parts, "期限": due}


def diag(problem, causes, evidence, confidence):
    return {"問題": problem, "可能原因": causes, "判斷證據": evidence, "可信度": confidence}


GAS = ("co", "hcho", "tvoc")
LABELS = {"thi": "成羊區 THI", "temp": "溫度", "rh": "濕度", "co2": "CO2", "nh3": "ID4 NH3",
          "nh3_avg": "成羊區平均 NH3", "temp_avg": "成羊區平均溫度", "pm25": "PM2.5", "pm10": "PM10",
          "co": "CO", "hcho": "HCHO", "tvoc": "TVOC", "noise": "噪音", "wind": "風速"}


def _sensor_actions(name, found, diags, decs, wos, trust_of):
    """通用偵測器的發現 → 診斷、決策、工單（依問題類型對應處置）。"""
    for f, d in found:
        diags.append(diag(*d))
        k = f.get("變數")
        lab = LABELS.get(k, k)
        t = f["類型"]
        if t == "卡值":
            wos.append(work_order(name, f"{lab} 感測模組檢查", "數據品質", d[2], 2, f"{lab} 感測模組"))
        elif t == "階躍":
            if f["鄰站同步"]:
                decs.append(decision(name, f"{lab} 量測", "以手持式儀器現場比對，確認是真實排放或感測器漂移", "3 天內", "—",
                                     d[2], "區分區域排放事件與感測器問題", "中", trust_of(k), False,
                                     f"比對前 {lab} 告警標示「待確認」", priority=2))
            else:
                decs.append(decision(name, f"{lab} 感測器", f"暫停 {lab} 告警判斷，零點校正", "7 天內", "—",
                                     d[2], f"避免誤報 {lab} 超標", "低", trust_of(k), False,
                                     f"校正前 {lab} 告警改為「待確認」等級，由人工判讀", priority=2))
            wos.append(work_order(name, f"{lab} 感測器零點校正與現場比對", "數據品質", d[2][:80], 2, "標準氣體"))
        elif t == "重啟":
            decs.append(decision(name, "告警規則", "斷線恢復後 2 小時內讀值不觸發告警", "設定變更", "—",
                                 d[2], "減少重啟暖機造成的誤報", "低", 100, True, "—", priority=3))
        elif t in ("飽和", "超出範圍"):
            wos.append(work_order(name, f"{lab} 感測器量程檢查", "數據品質", d[2][:80], 1, "—", "30 天內"))


def _now(site, col):
    """當下值：最後一筆可信讀值（含時間與可信度）。"""
    c, t = site["clean"], site["trust"]
    s = c[col].dropna() if col in c else pd.Series(dtype=float)
    if s.empty:
        return None, None, None
    ts = s.index[-1]
    return float(s.iloc[-1]), ts, float(t.loc[ts, col]) if col in t else None


def _trust_now(site, col, hours=6):
    t = site["trust"]
    return float(t[col].iloc[-hours:].mean()) if col in t else None


def _fc_summary(fc, name, unit=""):
    if not fc:
        return None
    return {"變數": name, "方法": fc["method"], "現值": fc["now"], "預測": [round(p, 2) for p in fc["pred"]],
            "下界": [round(p, 2) for p in fc["lo"]], "上界": [round(p, 2) for p in fc["hi"]],
            "模型MAE": [round(m, 3) for m in fc["mae_model"]], "持續法MAE": [round(m, 3) for m in fc["mae_naive"]],
            "改善率": round(fc["skill"] * 100, 1), "單位": unit,
            "模型版本": fc.get("version"), "準度追蹤": fc.get("tracking")}


def _effects():
    return json.loads(EFFECT_DB.read_text(encoding="utf-8")) if EFFECT_DB.exists() else {}


def _events_recent(an, days=14):
    if not an["events"]:
        return []
    end = an["flag"].index[-1]
    ev = [e for e in an["events"] if e["end"] >= end - pd.Timedelta(days=days)]
    return [{"開始": str(e["start"]), "時數": e["hours"], "強度z": round(e["peak"], 1),
             "主要變數": {k: f"{e['direction'][k]} z={v:.1f}" for k, v in e["vars"].items()}}
            for e in sorted(ev, key=lambda e: -e["peak"])[:5]]


def _anom_summary(site, an):
    contrib = {}
    for e in an["events"]:
        for k in e["vars"]:
            contrib[k] = contrib.get(k, 0) + e["hours"]
    return {"異常小時數": int(an["flag"].sum()), "總小時數": int(len(an["flag"])),
            "變數貢獻(小時)": dict(sorted(contrib.items(), key=lambda kv: -kv[1])[:6]),
            "事件數": len(an["events"]), "近14天事件": _events_recent(an)}


# =================================================================== 羊舍
FAN_COUNT = 4          # 羊舍可控風扇數（風扇-03／04／07／08）
FAN_HOLD_H = 4         # 預估開啟時數（決策輸出與成本估算用）


def fan_plan(peak, eff=None, n_avail=FAN_COUNT, hours=FAN_HOLD_H):
    """風扇台數推算。peak：現值與未來 6 小時預測的較高者；eff：成效資料庫中「風扇開啟」的驗證結果。

    目標：危險級（≥79）降到 79 以下；警戒級（74–78）降回 74 以下。
    ① 效果已驗證：每台效果 = 保守估計值（效果 + 1.645×標準誤，取較不樂觀的一端）÷ 平均同時開啟台數，
       需要台數 = 要降的量 ÷（每台每 2 小時效果 × 開啟時數 / 2），無條件進位，上限為現有台數。
    ② 效果未驗證：無法從資料推算台數。
       危險級 → 全開（保守）：多開一台的電費遠低於熱緊迫造成的損失，且全開才能累積成效樣本。
       警戒級 → 開一半（試驗）：與危險級的全開形成對照，累積「不同台數」的成效資料，之後才能推算每台效果。
    回傳 {"n", "模式", "說明", "足夠"}；足夠=False 代表全開仍不足以達標，需搭配其他降溫措施。"""
    if peak is None or pd.isna(peak) or peak < 74:
        return {"n": 0, "模式": "不需開啟", "說明": "THI 低於 74（正常）", "足夠": True}
    target = 79 if peak >= 79 else 74
    gap = peak - target + 0.1
    eff = eff or {}
    e, se, n_s = eff.get("effect"), eff.get("se"), eff.get("n", 0)
    fans_mean = eff.get("fans_mean") or n_avail
    cond = f"樣本 {n_s} 筆" + (f"，效果 {e:+.2f}±{se:.2f}" if e is not None and se is not None else "")
    if eff.get("verified") and e is not None and se is not None and e + Z_ONE_SIDED * se < 0:
        per_fan = -(e + Z_ONE_SIDED * se) / fans_mean          # 每台每 2 小時可多降的 THI（保守值）
        drop_each = per_fan * hours / 2
        need = int(np.ceil(gap / drop_each))
        n = min(n_avail, max(1, need))
        ok = need <= n_avail
        txt = (f"依成效資料推算（{cond}，已驗證）：每台約可多降 {per_fan:.2f}／2h，"
               f"要從 {peak:.1f} 降到 {target} 以下需降 {gap:.1f}，開 {hours} 小時需 {need} 台")
        if not ok:
            txt += f"；全開 {n_avail} 台預估只能降 {drop_each * n_avail:.1f}，需搭配噴霧、遮陽、增加飲水"
        return {"n": n, "模式": "成效推算", "說明": txt, "足夠": ok}
    rough = ""
    if e is not None and e < 0 and target == 79:
        drop_all = -e / fans_mean * n_avail * hours / 2
        rough = (f"；以目前不完整的資料粗估，全開 {hours} 小時約可多降 {drop_all:.1f}"
                 + (f"，可能不足以降到 {target} 以下" if drop_all < gap else "") + "（僅供參考）")
    if target == 79:
        cost = n_avail * FAN_KW * PRICE
        txt = (f"風扇效果尚未驗證（{cond}；需 ≥{MIN_SAMPLES} 筆且單尾 95% 顯著），無法從資料推算台數"
               f"{'；現場紀錄中 ' + format(eff['all_on_share'], '.0%') + ' 的開啟時段是全部同時開，無法區分台數差異' if eff.get('all_on_share') is not None else ''}"
               f" → 採保守策略全開：全開電費約 NT${cost:.0f}／小時，遠低於熱緊迫損失，且全開才能累積成效樣本{rough}")
        return {"n": n_avail, "模式": "保守全開", "說明": txt, "足夠": None}
    n = max(1, n_avail // 2)
    txt = (f"警戒級且風扇效果尚未驗證（{cond}）→ 試驗開 {n} 台，與危險級全開 {n_avail} 台形成對照，"
           f"累積不同台數的成效資料，之後即可推算每台效果{rough}")
    return {"n": n, "模式": "試驗", "說明": txt, "足夠": None}


def sheep_rule(row) -> int:
    """建議開啟風扇台數（閉環回放用：以效果未驗證時的規則回放，與現場實際操作對照）。"""
    return fan_plan(row.get("thi"))["n"]


def build_sheep(site, ctx):
    cfg, name = site["cfg"], site["cfg"]["name"]
    thi, ts, tr = _now(site, "thi")
    temp, _, _ = _now(site, "temp_avg")
    rh, _, _ = _now(site, "rh_avg")
    lvl = A.level_of(thi, cfg["thresholds"]["thi"])
    fc = A.forecast(site["clean"]["thi"], key="sheep:thi")
    peak6 = max(fc["pred"]) if fc else thi
    eta79 = A.time_to_threshold(fc, 79)
    # 成效只用「當下」以前的現場紀錄計算（回放時樣本會隨時間累積，即閉環再學習）
    eff = {e["action"]: e for e in F.sheep_effects(site)[0]}
    fan_eff = eff.get("風扇開啟", {})
    shade_eff = eff.get("南遮陽張開", {})
    rain = site["df"]["rain_1h"].iloc[-1] if "rain_1h" in site["df"] else 0
    hour = ts.hour
    nh3_trust = _trust_now(site, "nh3_avg", 24)
    replay = ctx["replay"]
    ver = ctx["verify"]
    abn = [h for h in ver["abnormal_hours"] if pd.Timestamp(h) <= ts]

    status = {"指數": "THI（溫濕指數）", "數值": round(thi, 1), "等級": A.LEVEL_NAME[lvl], "等級碼": lvl,
              "時間": str(ts), "感測可信度": round(tr),
              "讀值": {"成羊區THI": thi, "仔羊區THI": _now(site, "thi_lamb")[0], "平均溫度": temp,
                     "平均濕度": rh, "ID4 CO2": _now(site, "co2")[0], "ID4 NH3": _now(site, "nh3")[0]}}

    diags = [
        diag("持續熱緊迫", "夏季高溫高濕；舍內通風換氣量不足",
             f"至今 {replay['hours']} 小時中 THI≥79（危險）{replay['hours_thi_danger']} 小時"
             f"（{replay['hours_thi_danger']/replay['hours']:.0%}），≥84（緊急）{replay['hours_thi_emergency']} 小時；"
             f"每日 13–15 時平均最高", "高"),
        diag("建議與執行脫節", "現場未依系統建議開風扇（人工操作或排程未連動）",
             f"規則建議開風扇 {replay['hours_need']} 小時，實際開啟僅 {replay['hours_actual']} 小時"
             f"（執行率 {replay['hours_actual']/max(1,replay['hours_need']):.1%}）；"
             f"既有「加開」旗標亮起 {replay['addon_flag_hours']} 小時", "高"),
    ]
    nh3_stuck = G.stuck_value(site["df"]["nh3_avg"])
    id4 = site["clean"]["nh3"].dropna()
    id4_txt = f"{id4.quantile(0.05):.2f}–{id4.quantile(0.95):.2f} ppm" if len(id4) else "無資料"
    if nh3_stuck:
        p_, c_, e_, conf_ = G.describe(nh3_stuck, "成羊區平均 NH3")
        diags.append(diag(p_, "平均值計算未納入有效探頭，或 NH3 感測器故障",
                          e_ + f"；同時段 ID4 NH3 有讀值（5–95% 區間 {id4_txt}）", conf_))
    if abn:
        diags.append(diag("控制器異常碼", "控制器／通訊模組回報異常（所有設備同時出現 2/4 碼）",
                          f"異常碼時段：{', '.join(abn[:2])} … 共 {len(abn)} 小時，全部 16 個設備點位同時異常", "中"))

    plan = fan_plan(max(thi, peak6), fan_eff)
    fans = plan["n"]
    decs = []
    if fans:
        hrs = FAN_HOLD_H
        decs.append(decision(
            name, "風扇-03／04／07／08", "開啟", f"立即，至 THI 連續 2 小時 < 79（預估 {hrs} 小時）",
            f"{fans}/{FAN_COUNT} 台（{plan['模式']}）",
            f"THI {thi:.1f}（{A.LEVEL_NAME[lvl]}），未來 6 小時預測最高 {peak6:.1f}"
            + (f"，{eta79} 小時後達危險級" if eta79 and thi < 79 else ""),
            plan["說明"], "低", tr, bool(fan_eff.get("verified")),
            "THI 讀值不可信 → 改用平均溫濕度重算 THI；溫濕度也失效 → 依時段排程 10–17 時全開",
            cost=f"約 NT${fans*FAN_KW*hrs*PRICE:.0f}（{fans} 台×{FAN_KW}kW×{hrs}h，電價假設 NT${PRICE}/kWh）",
            priority=1 if lvl >= 2 else 2))
    if 9 <= hour <= 16 and (temp or 0) >= 30:
        decs.append(decision(
            name, "南遮陽 25/26", "張開", "立即，至 16 時", "全幅",
            f"日照時段（{hour} 時）且舍溫 {temp:.1f}°C",
            f"歷史回饋：遮陽張開時 THI 比同時段平常 {shade_eff.get('effect', 0):+.2f}（前後 2 小時變化量，n={shade_eff.get('n',0)}，未見顯著效果）",
            "低", tr, bool(shade_eff.get("verified")), "遮陽馬達無回授 → 維持現狀並派員目視", priority=3))
    if rain and rain > 0:
        decs.append(decision(name, "南／北／西帆布", "張開（擋雨）", "立即", "全幅",
                             f"1 小時雨量 {rain} mm", "避免雨水打入、墊料潮濕", "中", tr, False,
                             "帆布馬達異常 → 人工手動操作", priority=2))
    else:
        decs.append(decision(name, "南／北／西帆布", "維持收起（通風）", "持續", "—",
                             "無降雨，帆布收起以利自然通風", "維持換氣量", "中", tr, False,
                             "遇雨或強風（風速>8 m/s）→ 人工確認後張開", priority=3))
    if lvl == 3 or peak6 >= 84 or plan["足夠"] is False:
        why = (f"THI 達或預測達緊急級（{max(thi, peak6):.1f}）" if (lvl == 3 or peak6 >= 84)
               else f"依成效資料推算，風扇全開仍不足以讓 THI 從 {max(thi, peak6):.1f} 降到目標以下")
        decs.append(decision(name, "飲水／噴霧（人工）", "增加飲水供應、啟動噴霧降溫", "立即",
                             "全區", why,
                             "降低熱休克與採食下降風險", "高", tr, False, "現場人員巡檢羊隻呼吸頻率", priority=1))
    if nh3_stuck:
        decs.append(decision(name, "成羊區 NH3 量測", "停用平均值，改用 ID4 NH3", "立即", "—",
                             "成羊區平均NH3 讀值無反應，可信度 {:.0f}".format(nh3_trust or 0),
                             "避免以失效數據判斷氨氣風險", "低", nh3_trust, False,
                             f"以 ID4 NH3（{id4_txt}）代表；派工校正", priority=2))
    wos = [] if not nh3_stuck else [
        work_order(name, "成羊區平均 NH3 感測器檢修／校正", "數據品質",
                   f"平均 NH3 {nh3_stuck['比例']:.0%} 時間為 {nh3_stuck['數值']:g}，需確認探頭接線與平均值計算設定", 2, "NH3 感測模組")]
    wos += [
        work_order(name, "風扇自動連動設定", "生產",
                   f"建議開風扇 {replay['hours_need']} 小時、實際 {replay['hours_actual']} 小時；將 THI≥79 連動風扇（試行期人工確認）", 3,
                   due="3 天內"),
    ]
    if abn:
        wos.append(work_order(name, "控制器異常碼檢查", "生產", f"{len(abn)} 小時所有點位同時回報 2/4 碼，檢查控制器與通訊", 2))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc, "成羊區 THI")], "eta": {"THI達79(h)": eta79}}


# =================================================================== 專家系統（水產池＋馬達）
def build_expert(site, ctx):
    cfg, name = site["cfg"], site["cfg"]["name"]
    df, c, t = site["df"], site["clean"], site["trust"]
    ts = df.index[-1]
    # 備援表決：只用可信探棒的中位數
    probes = {}
    for g, cols in cfg["groups"].items():
        good = [k for k in cols if t[k].iloc[-6:].mean() >= TRUST_AUTO and pd.notna(c[k].iloc[-1])]
        probes[g] = {"可信探棒": good, "排除": [k for k in cols if k not in good],
                     "共識值": float(np.median([c[k].iloc[-1] for k in good])) if good else None,
                     "原始值": {k: (None if pd.isna(df[k].iloc[-1]) else float(df[k].iloc[-1])) for k in cols}}
    do = probes["DO"]["共識值"]
    ph = probes["pH"]["共識值"]
    lvl = A.level_of(do, cfg["thresholds"]["do"], lower_is_bad=True)
    w = A.wqi(do=do, ph=ph)
    fc_do = A.forecast(c["do1"], key="expert:do1")
    fc_cur = A.forecast(c["current"], key="expert:current")
    motor = A.motor_stop_model(df, key="expert:motor")
    mstop = F.verify_expert(site)          # 只用當下以前的資料驗證馬達成效
    pf = float(df["pf"].iloc[-168:].mean())
    kwh7 = (float(df["kwh"].iloc[-1] - df["kwh"].iloc[-169])
            if len(df) >= 169 and df["kwh"].iloc[-169] > 0 else None)   # 不足 7 天資料時不計算
    sc_eta, _ = A.linear_eta(c["sc1"], 2.0)

    status = {"指數": "WQI（簡化水質指數，DO＋pH）", "數值": w, "等級": A.LEVEL_NAME[lvl or 0], "等級碼": lvl or 0,
              "時間": str(ts), "感測可信度": round(np.mean([t[k].iloc[-6:].mean() for k in probes["DO"]["可信探棒"]])) if probes["DO"]["可信探棒"] else 0,
              "讀值": {"DO共識": do, "pH共識": ph, "ORP共識": probes["ORP"]["共識值"], "SC共識": probes["SC"]["共識值"],
                     "水溫": _now(site, "wtemp")[0], "水位": _now(site, "level")[0],
                     "馬達電流": float(df["current"].iloc[-1]), "功率因數": float(df["pf"].iloc[-1])},
              "備援表決": probes}

    bad_do = probes["DO"]["排除"]
    bad_sc = probes["SC"]["排除"]
    ph1_n = int(site["reason"]["ph1"].str.contains("pH探棒不一致").sum())
    sc_day = c[["sc1", "sc2", "sc3"]].resample("D").mean().dropna(how="all")
    diags = []
    for k in bad_do:
        diags.append(diag(f"{k.upper()} 探棒失效", "探棒膜片汙損／老化或接線異常",
                          f"{k.upper()} 現值 {df[k].iloc[-1]:.2f}，三支 DO 為 "
                          + "、".join(f"{x.upper()} {df[x].iloc[-1]:.2f}" for x in cfg['groups']['DO'])
                          + "；與三支中位數差 >1.5 → 判定為探棒問題，非水體缺氧", "高"))
    if bad_sc or (len(sc_day) > 30 and sc_day.iloc[-7:].mean().mean() < 0.7 * sc_day.iloc[:7].mean().mean()):
        diags.append(diag("SC 探棒漂移", "電極結垢、需校正",
                          f"三支 SC 日均值由 {sc_day.iloc[:7].mean().mean():.1f} 降至 {sc_day.iloc[-7:].mean().mean():.1f}；"
                          f"SC1 現值 {df['sc1'].iloc[-1]:.2f}" + ("，已偏離另兩支" if "sc1" in bad_sc else ""), "中"))
    if ph1_n > 0:
        diags.append(diag("pH1 偏移", "pH1 參考電極老化", f"至今 pH1 與另兩支差異 >0.5 的時數 {ph1_n} 小時", "中"))
    if motor["n_stop_hours"] > 0:
        diags.append(diag("日間停機時 DO 偏低", "日間停機排程（推測為節能）在低 DO 日仍執行",
                          f"至今停機 {motor['n_stop_hours']} 小時，集中於 {', '.join(str(h) for h in motor['peak_hours'])} 時；"
                          f"與同時段運轉小時相比，停機時 DO 低 {-mstop['level_gap_same_hour']:.2f} mg/L（中位數）", "中"))
    if pf < 0.85:
        diags.append(diag("功率因數偏低", "感應馬達負載、未設電容補償", f"近 7 日平均功率因數 {pf:.2f}", "高"))

    decs = []
    tr_do = status["感測可信度"]
    if lvl and lvl >= 1:
        decs.append(decision(
            name, "CPM 馬達（打氣／循環）", "保持運轉，暫停日間停機排程", "立即，至 DO 共識值連續 3 小時 ≥ 4.5",
            "100% 連續運轉", f"DO 共識值 {do:.2f} mg/L（{A.LEVEL_NAME[lvl]}，<4）；6 小時預測區間 {min(fc_do['lo']):.2f}–{max(fc_do['hi']):.2f}",
            f"歷史回饋：停機時 DO 比同時段運轉低 {-mstop['level_gap_same_hour']:.2f} mg/L（n={mstop['n']}），持續運轉可避免此落差",
            "低", tr_do, mstop["verified"],
            "DO 探棒全部不可信 → 保守策略：馬達維持 24h 運轉並派員以手持式 DO 計量測",
            cost=f"多運轉約 {mstop['stop_hours_per_day']:.1f} h/日 × {float(df['power'].median())/1000:.2f} kW", priority=1))
    elif do is None:
        decs.append(decision(name, "CPM 馬達（打氣／循環）", "保守運轉 24 小時", "立即", "100%",
                             "三支 DO 探棒皆不可信，無法判斷溶氧", "避免在無量測時缺氧", "低", 0, False,
                             "派員以手持式 DO 計量測", priority=1))
    else:
        decs.append(decision(name, "CPM 馬達", "維持現行排程", "持續", "—", f"DO 共識值 {do:.2f} 正常",
                             "—", "低", tr_do, True, "—", priority=3))
    for k in bad_do:
        good = "／".join(x.upper() for x in probes["DO"]["可信探棒"]) or "無"
        decs.append(decision(name, f"{k.upper()} 探棒", f"自控制邏輯中排除，改用 {good} 表決", "立即", "—",
                             f"{k.upper()} 與另兩支不一致", "避免誤判缺氧而過度打氣／誤告警", "低",
                             _trust_now(site, k), False, "其餘探棒也失效 → 馬達 24h 運轉＋人工量測", priority=2))
    for k in bad_sc:
        decs.append(decision(name, f"{k.upper()} 探棒", "自控制邏輯中排除", "立即", "—", f"{k.upper()} 與另兩支 SC 不一致",
                             "SC 以其餘探棒中位數判斷", "低", _trust_now(site, k), False, "派工校正", priority=3))
    decs.append(decision(name, "CPM 馬達", (f"列入例行巡檢（模型估 24h 停機機率 {motor['p_now']:.1%}）" if motor.get("version") is None or motor["version"]["重訓次數"]
                          else f"列入例行巡檢（模型尚未訓練，歷史停機頻率 {motor['p_now']:.1%}）"), "下次例行巡檢",
                         "—", f"近 7 日平均電流 {motor['cur_mean7d']:.2f} A（全期 {motor['cur_mean_all']:.2f} A），無劣化趨勢",
                         "維持設備可用率", "低", _trust_now(site, "current"), True, "—", priority=3))

    wos = []
    for k in bad_do:
        wos.append(work_order(name, f"{k.upper()} 探棒清洗／更換", "生產", f"{k.upper()} 讀值與另兩支不一致，已自控制邏輯排除", 3,
                              "DO 探棒（膜片／電解液）", "3 天內"))
    if any(d["問題"] == "SC 探棒漂移" for d in diags):
        wos.append(work_order(name, "SC1–SC3 探棒校正", "數據品質",
                              "三支 SC 持續下降" + (f"；SC1 現值 {df['sc1'].iloc[-1]:.2f} 已低於 2" if df['sc1'].iloc[-1] < 2
                                               else (f"；SC1 依趨勢約 {sc_eta:.0f} 天後低於 2" if sc_eta else "")),
                              2, "標準電導液"))
    if ph1_n > 24:
        wos.append(work_order(name, "pH1 校正", "數據品質", f"pH1 與 pH2／pH3 差 >0.5 共 {ph1_n} 小時", 1, "pH 4/7 緩衝液"))
    if pf < 0.85:
        wos.append(work_order(name, "功率因數改善評估", "能耗", f"平均 PF {pf:.2f}；評估加裝進相電容至 0.95", 1, "進相電容", "30 天內"))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc_do, "DO1", "mg/L"), _fc_summary(fc_cur, "馬達電流", "A")],
            "equipment": {"馬達24h停機機率": round(motor["p_now"], 3), "測試AUC": None if motor["auc"] is None else round(motor["auc"], 3),
                          "基準停機率": round(motor["base_rate"], 3),
                          "停機模型": motor.get("method"), "模型重訓次數": (motor.get("version") or {}).get("重訓次數"), "近7日用電kWh": None if kwh7 is None else round(kwh7, 1),
                          "平均功率因數": round(pf, 3)}}


# =================================================================== 三爺宮溪
def build_river(site, ctx):
    cfg, name = site["cfg"], site["cfg"]["name"]
    df, c = site["df"], site["clean"]
    ts = df.index[-1]
    last48 = df["do"].iloc[-48:]
    rep = last48.round(2).value_counts()
    floor48 = float((last48 <= 0.05).mean())           # 讀值落在量測下限 0.05 的比例
    floor_hist = float((df["do"] <= 0.05).mean())
    do_med48 = float(last48.median())
    ph, _, _ = _now(site, "ph")
    ec, _, _ = _now(site, "ec")
    trust_do = _trust_now(site, "do", 48)
    # 讀值只在少數幾個數字間跳動 → 降低可信度（探棒異常特徵）
    trust_do_eff = trust_do * (0.6 if floor48 > 0.3 else 1)
    lvl = A.level_of(do_med48, cfg["thresholds"]["do"], lower_is_bad=True)
    w = A.wqi(do=do_med48, ph=ph, ec=ec)
    fc_do = A.forecast(c["do"], key="river:do")
    fc_ec = A.forecast(c["ec"], key="river:ec")
    month = df["do"].resample("MS").median()
    diurnal = c["do"].groupby(c.index.hour).median()
    batt_min = df["battery"].resample("D").min()
    solar = df["solar_v"].resample("D").max().iloc[:-1]
    low_solar_days = int((solar.iloc[-30:] < 10).sum())
    cal_days = df[["ph_slope", "ec_slope", "do_slope"]].diff().ne(0).any(axis=1)
    cal_days = sorted({str(d.date()) for d in cal_days[cal_days].index[1:]})

    status = {"指數": "WQI（簡化水質指數，DO＋pH＋EC）", "數值": w, "等級": A.LEVEL_NAME[lvl], "等級碼": lvl,
              "時間": str(ts), "感測可信度": round(trust_do_eff),
              "讀值": {"DO(近48h中位數)": do_med48, "pH": ph, "EC": ec, "水溫": _now(site, "wtemp")[0],
                     "電池電量": float(df["battery"].iloc[-1]), "電池電壓": float(df["batt_v"].iloc[-1])}}

    diags = [
        diag("DO 長期變化", "①真實有機污染負荷增加；②DO 探棒汙損老化（兩者並存可能）",
             "月中位數 " + "→".join(f"{d.month}月 {v:.2f}" for d, v in month.items())
             + f"；日夜週期仍在（午後 15 時 {diurnal.max():.2f}、夜間 {diurnal.min():.2f}，符合光合作用特徵，支持①）", "中"),
    ]
    if floor48 > 0.3:
        diags.append(diag("DO 探棒讀值異常", "探棒膜片汙損／訊號不穩",
                          f"近 48 小時 {floor48:.0%} 讀值落在量測下限 0.05（至今 {floor_hist:.0%}），"
                          f"其餘集中在 {', '.join(str(v) for v in rep.drop(0.05, errors='ignore').head(3).index)} 等少數數字間跳動（支持②）", "中"))
    if low_solar_days >= 3:
        recent = batt_min.iloc[-11:-1] if len(batt_min) > 2 else batt_min
        diags.append(diag("太陽能供電不足", "陰雨或太陽能板遮蔽／髒污", f"近 30 日 {low_solar_days} 天太陽能最高電壓 <10V；"
                          f"電池每日最低電量近 10 天 {int(recent.min())}–{int(recent.max())}%", "中"))
    probe_bad = floor48 > 0.3
    decs, wos = [], []
    if lvl >= 2:
        decs.append(decision(name, "水質通報（人工）", "通報環保單位並加密人工採樣", "24 小時內", "每日 2 次（07、15 時）",
                             f"DO 近 48h 中位數 {do_med48:.2f} mg/L（RPI {A.LEVEL_NAME[lvl]}級）",
                             "確認是否為真實污染事件、追查上游排放", "中", trust_do_eff, False,
                             "探棒可信度不足 → 以人工手持式 DO 計採樣結果為準", priority=1))
    elif lvl == 1:
        decs.append(decision(name, "水質告警", "發布輕度污染告警、提高監測頻率", "立即", "—",
                             f"DO 近 48h 中位數 {do_med48:.2f} mg/L（RPI 輕度污染）", "提早掌握惡化趨勢", "低",
                             trust_do_eff, True, "探棒不可信 → 人工採樣", priority=2))
    else:
        decs.append(decision(name, "水質監測", "維持監測", "持續", "—", f"DO 近 48h 中位數 {do_med48:.2f} mg/L（未受污染）",
                             "—", "低", trust_do_eff, True, "探棒不可信 → 人工採樣", priority=3))
    if probe_bad:
        decs.append(decision(name, "DO 探棒", "現場清洗＋校正，校正前後各量一次比對", "3 天內", "—",
                             "讀值集中在量測下限、與長期下降趨勢需區分真偽", "判斷 DO 下降中有多少屬於探棒問題", "低",
                             trust_do_eff, False, "無法到場 → 告警標註「DO 數據待確認」", priority=1))
        wos.append(work_order(name, "DO 探棒清洗／校正並人工比對", "數據品質", "決定 DO 嚴重缺氧是真實事件或探棒問題", 3,
                              "DO 膜片、校正液", "3 天內"))
    if low_solar_days >= 3:
        wos.append(work_order(name, "太陽能板清潔與供電檢查", "生產",
                              f"近 30 日 {low_solar_days} 天發電偏低，電池最低 {int(batt_min.min())}%", 2, "—", "7 天內"))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc_do, "DO", "mg/L"), _fc_summary(fc_ec, "EC", "µS/cm")],
            "maintenance_log": {"推定校正日（斜率改變）": cal_days[:8], "總數": len(cal_days)}}


# =================================================================== 空品三站
def build_air(site, ctx):
    cfg, name, sid = site["cfg"], site["cfg"]["name"], site["id"]
    df, c, raw = site["df"], site["clean"], site["raw"]
    ts = df.index[-1]
    completeness = float(raw.notna().any(axis=1).mean())
    pm24 = c["pm25"].iloc[-24:].mean()
    trust_pm = _trust_now(site, "pm25", 24)
    used_peer = None
    if pd.isna(pm24) or (trust_pm or 0) < TRUST_FALLBACK:
        # Fallback：本站不可信 → 借用同區鄰站
        for p in AIR_PEERS:
            if p != sid:
                ps = ctx["sites"][p]["clean"]["pm25"]
                v = ps.loc[:ts].iloc[-24:].mean()
                if pd.notna(v):
                    pm24, used_peer = v, ctx["sites"][p]["cfg"]["name"]
                    break
    aqi = A.aqi_pm25(pm24)
    lvl = A.level_of(pm24, cfg["thresholds"]["pm25"])
    fc = A.forecast(c["pm25"], key=f"{sid}:pm25") if "pm25" in cfg["forecast"] else None
    vals = {k: _now(site, k)[0] for k in ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"]}
    status = {"指數": "AQI（PM2.5 子指標，24h 平均）", "數值": aqi, "等級": A.LEVEL_NAME[lvl or 0], "等級碼": lvl or 0,
              "時間": str(ts), "感測可信度": round(trust_pm or 0), "資料完整率": round(completeness * 100, 1),
              "讀值": vals, "PM2.5(24h)": None if pd.isna(pm24) else round(float(pm24), 1),
              "Fallback來源": used_peer}
    diags, decs, wos = [], [], []
    # 感測器問題：由通用偵測器從資料中找出（不寫死日期或數值）
    peers = {k: {ctx["sites"][p]["cfg"]["name"].split("（")[0]: ctx["sites"][p]["df"][k]
                 for p in AIR_PEERS if p != sid} for k in GAS}
    found = G.sensor_findings(df, cfg, LABELS, gas=GAS, peers=peers, cuts=cfg["thresholds"],
                              temp=df["temp"], restart_cols=GAS + ("pm25", "pm10"))
    _sensor_actions(name, found, diags, decs, wos, lambda k: _trust_now(site, k, 24))
    if sid == "breed1_north":
        boots = df["boots"].dropna()
        diags.append(diag("資料完整率過低", "供電中斷（太陽能／市電）或通訊中斷後未自動補傳",
                          f"{df.index[0].month}/{df.index[0].day}–{ts.month}/{ts.day} 僅 {completeness:.1%} 的小時有資料；"
                          f"開機次數由 {int(boots.min()) if len(boots) else 0} 增至 {int(boots.max()) if len(boots) else 0}；"
                          f"4G 訊號 CSQ 中位數 {df['csq'].median():.0f}（良好）→ 較可能是供電問題", "高"))
        decs.append(decision(name, "本站空品判斷", f"改用鄰站（{used_peer or '育成一廠'}）數據", "立即", "—",
                             "本站可信度不足", "維持區域空品監測不中斷", "低", trust_pm, False,
                             f"以鄰站 {used_peer or '育成一廠'} 24h PM2.5 代表", priority=2))
        wos.append(work_order(name, "北側站供電與資料補傳檢修", "數據品質",
                              f"完整率 {completeness:.1%}、多次重開機、最後一筆全 0", 3, "電源模組／電池", "3 天內"))
    if used_peer:
        pass  # 本站已改走 Fallback（借用鄰站），不再重複產生監測決策
    elif lvl and lvl >= 1:
        decs.append(decision(name, "空品告警", "發布廠區空品警示，減少戶外作業", "立即", "—",
                             f"PM2.5 24h 平均 {pm24:.1f}（AQI {aqi}）", "降低人員暴露", "中", trust_pm, False,
                             "以鄰站數據判斷", priority=1))
    else:
        decs.append(decision(name, "空品監測", "維持監測", "持續", "—",
                             f"PM2.5 24h 平均 {pm24:.1f} µg/m³（AQI {aqi}，正常）", "—", "低", trust_pm, True,
                             "本站失效時以鄰站代表", priority=3))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc, "PM2.5", "µg/m³")] if fc else []}


# =================================================================== 工地微氣候
def build_site(site, ctx):
    cfg, name = site["cfg"], site["cfg"]["name"]
    df, c = site["df"], site["clean"]
    ts = df.index[-1]
    pm10, _, tr = _now(site, "pm10")
    noise = _now(site, "noise")[0]
    lvl = A.level_of(pm10, cfg["thresholds"]["pm10"])
    fc = A.forecast(c["pm10"], key="site:pm10")
    eta = A.time_to_threshold(fc, cfg["thresholds"]["pm10"][0])
    over = df["pm10"] >= cfg["thresholds"]["pm10"][0]
    rh_hi = df["rh"] >= 85
    loud = (df["noise"] >= cfg["thresholds"]["noise"][0])
    loud_night = loud & ((df.index.hour >= 22) | (df.index.hour < 6))
    heat = int((df["hazard"] > 0).sum())
    status = {"指數": "PM10 分級＋噪音＋熱危害", "數值": None if pm10 is None else round(pm10, 1),
              "等級": A.LEVEL_NAME[lvl or 0], "等級碼": lvl or 0, "時間": str(ts), "感測可信度": round(tr or 0),
              "讀值": {"PM10": pm10, "PM2.5": _now(site, "pm25")[0], "噪音": noise, "溫度": _now(site, "temp")[0],
                     "濕度": _now(site, "rh")[0], "風速": _now(site, "wind")[0]},
              "設定門檻": site["meta"]}
    diags, s_decs, wos = [], [], []
    found = G.sensor_findings(df, cfg, LABELS, cuts=cfg["thresholds"])
    pm_bad = [f for f, _ in found if f.get("變數") in ("pm10", "pm25")]
    _sensor_actions(name, [x for x in found if x[0].get("變數") not in ("pm10", "pm25")], diags, s_decs, wos,
                    lambda k: _trust_now(site, k, 24))
    for f, d in found:
        if f.get("變數") in ("pm10", "pm25"):
            diags.append(diag(*d))
    diags += [
        diag("高濕時 PM 偏高", "光學式粉塵計在高濕度下吸濕高估，或逆溫擴散不良",
             f"PM10 超過 {cfg['thresholds']['pm10'][0]} 的 {int(over.sum())} 小時中，{(over & rh_hi).sum()/max(1,over.sum()):.0%} 發生在濕度 ≥85%", "中"),
        diag("夜間噪音", "夜間施工或機具運轉", f"夜間（22–6 時）噪音 ≥{cfg['thresholds']['noise'][0]} dB 共 {int(loud_night.sum())} 小時", "中"),
    ]
    decs = []
    if eta is not None or (lvl and lvl >= 1):
        decs.append(decision(name, "灑水／霧化降塵", "啟動", "立即" if lvl else f"{eta} 小時內", "周界全段",
                             f"PM10 現值 {pm10:.0f}（{A.LEVEL_NAME[lvl or 0]}）"
                             + (f"，預測 {eta} 小時後超過 {cfg['thresholds']['pm10'][0]}" if eta and not lvl else ""), "抑制揚塵",
                             "低", tr, False, "粉塵計失效 → 依施工時段排程灑水", priority=1))
    else:
        decs.append(decision(name, "灑水／霧化降塵", "待命（不啟動）", "持續監測", "—",
                             f"PM10 現值 {pm10:.0f}，6 小時內預測最高 {max(fc['pred']):.0f}（<{cfg['thresholds']['pm10'][0]}）", "節省用水",
                             "低", tr, True, "粉塵計失效 → 依施工時段排程灑水", priority=3))
    decs.append(decision(name, "作業調整（人工）", "熱危害等級 ≥2 時暫停戶外重體力作業", "依熱指數即時判斷",
                         "每小時休息 15 分鐘起", f"至今熱危害時數 {heat} 小時，現值等級 {int(df['hazard'].iloc[-1]) if pd.notna(df['hazard'].iloc[-1]) else 0}", "預防熱傷害",
                         "高", tr, False, "熱指數失效 → 依氣象局高溫資訊", priority=3))
    decs += s_decs
    if pm_bad:
        f = pm_bad[-1]
        wos.append(work_order(name, "粉塵計檢查（極端值＋高濕修正）", "數據品質",
                              f"{f['開始'].month}/{f['開始'].day} 出現 {LABELS.get(f['變數'])} {f['數值']:g}，超出合理範圍；評估加裝除濕進氣或濕度修正",
                              1, "—", "30 天內"))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc, "PM10", "µg/m³")], "eta": {f"PM10達{cfg['thresholds']['pm10'][0]}(h)": eta}}


BUILDERS = {"sheep": build_sheep, "expert": build_expert, "river": build_river,
            "breed1": build_air, "breed1_north": build_air, "northeast": build_air, "site": build_site}
