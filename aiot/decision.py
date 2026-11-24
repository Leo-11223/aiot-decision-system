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

# ---- 成本假設（現場可替換）：風扇單台 0.75 kW、電價 NT$4/kWh
FAN_KW, PRICE = 0.75, 4.0


def gate(risk: str, trust: float, verified: bool):
    if trust is None or trust < TRUST_FALLBACK:
        return "Fallback", True
    if risk == "低" and verified and trust >= TRUST_AUTO:
        return "自動執行", False
    return "人工確認", True


def decision(site, device, action, when, magnitude, reason, effect, risk, trust, verified, fallback,
             cost="—", priority=2, noop=False):
    # noop：維持現狀／待命／監測這類「不動作」的項目，不需核准也不下發，與「自動執行」區分
    mode, confirm = ("維持現狀", False) if noop else gate(risk, trust, verified)
    return {"場域": site, "設備": device, "動作": action, "時間": when, "幅度": magnitude, "理由": reason,
            "預期效果": effect, "風險": risk, "感測可信度": None if trust is None else round(float(trust)),
            "成效已驗證": verified, "執行模式": mode, "人工確認": confirm, "Fallback": fallback,
            "成本": cost, "優先": priority}


def episode_start(thi, low=73.5, hold=2):
    """這段高溫從什麼時候開始：往回找最近一次「THI 連續 hold 小時低於 low」（風扇解除條件）之後的第一個小時。
    只用到當下以前的資料。整段都沒解除過 → 資料起點。"""
    s = thi.dropna()
    if s.empty:
        return None
    ok = (s < low).astype(int).rolling(hold).sum() >= hold
    hit = ok[ok].index
    start = s.index[0] if not len(hit) else hit[-1] + pd.Timedelta(hours=1)
    return start.strftime("%Y-%m-%dT%H:%M")


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
            decs.append(decision(name, "告警規則", "建議設定：斷線恢復後 2 小時內讀值不觸發告警", "設定變更", "—",
                                 d[2], "減少重啟暖機造成的誤報", "低", trust_of(k), False,
                                 "未設定前，重啟後的告警由人工判讀", priority=3))
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


def _f(v, fmt=".1f", na="—"):
    """數值轉文字；None／NaN 顯示為「—」，避免決策文字出現 nan。"""
    try:
        return na if v is None or pd.isna(v) else format(float(v), fmt)
    except (TypeError, ValueError):
        return na


def _peak(fc, now, lower=False):
    """預測的最高（或最低）值；預測不可用時退回現值。"""
    vals = [p for p in (fc or {}).get("pred", []) if p is not None and not pd.isna(p)]
    if not vals:
        return now
    return min(vals) if lower else max(vals)


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
               f"{'；現場紀錄中 ' + format(eff['all_on_share'], '.0%') + ' 的開啟時段是全部同時開，無法區分台數差異' if eff.get('all_on_share') is not None and n_s >= 5 else ''}"
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
    peak6 = _peak(fc, thi)
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

    diags = []
    # 以下兩項都由「至今」的資料計算，達條件才輸出（不寫死結論）
    danger_share = replay["hours_thi_danger"] / max(1, replay["hours"])
    if replay["hours"] >= 24 and danger_share >= 0.5:
        hot = site["clean"]["thi"].groupby(site["clean"].index.hour).mean().nlargest(3).sort_index()
        diags.append(diag("持續熱緊迫", "高溫高濕；舍內通風換氣量不足",
                          f"至今 {replay['hours']} 小時中 THI≥79（危險）{replay['hours_thi_danger']} 小時"
                          f"（{danger_share:.0%}），≥84（緊急）{replay['hours_thi_emergency']} 小時；"
                          f"平均 THI 最高的時段為 {', '.join(str(h) for h in hot.index)} 時", "高"))
    exec_rate = replay["hours_need_and_actual"] / max(1, replay["hours_need"])
    gap_exec = replay["hours_need"] >= 24 and exec_rate < 0.5
    if gap_exec:
        diags.append(diag("建議與執行脫節", "現場未依系統建議開風扇（人工操作或排程未連動）",
                          f"規則建議開風扇 {replay['hours_need']} 小時，其中實際有開 {replay['hours_need_and_actual']} 小時"
                          f"（執行率 {exec_rate:.1%}）"
                          + (f"；既有「加開」旗標亮起 {replay['addon_flag_hours']} 小時" if replay.get("addon_flag_hours") else ""),
                          "高"))
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
    # 現場風扇目前開了幾台（最後一筆設備紀錄）：已經夠了就不再要求人重新下令
    on_now = int((site["raw"][F.FANS].iloc[-1] == 1).sum()) if set(F.FANS) <= set(site["raw"].columns) else 0
    if fans and on_now >= fans:
        decs.append(decision(
            name, "風扇-03／04／07／08", "維持運轉",
            f"持續，至 THI 連續 2 小時 < {79 if max(thi, peak6) >= 79 else 74}", f"{on_now}/{FAN_COUNT} 台運轉中",
            f"THI {thi:.1f}（{A.LEVEL_NAME[lvl]}），未來 6 小時預測最高 {peak6:.1f}；現場已有 {on_now} 台運轉，不需再次下令",
            plan["說明"], "低", tr, bool(fan_eff.get("verified")), "—", priority=3, noop=True))
    elif fans:
        hrs = FAN_HOLD_H
        decs.append(decision(
            name, "風扇-03／04／07／08", "開啟",
            f"立即，至 THI 連續 2 小時 < {79 if max(thi, peak6) >= 79 else 74}（預估 {hrs} 小時）",
            f"{fans}/{FAN_COUNT} 台（{plan['模式']}）",
            f"THI {thi:.1f}（{A.LEVEL_NAME[lvl]}），未來 6 小時預測最高 {peak6:.1f}"
            + (f"，{eta79} 小時後達危險級" if eta79 and thi < 79 else ""),
            plan["說明"], "低", tr, bool(fan_eff.get("verified")),
            "THI 讀值不可信 → 改用平均溫濕度重算 THI；溫濕度也失效 → 依時段排程 10–17 時全開",
            cost=f"約 NT${fans*FAN_KW*hrs*PRICE:.0f}（{fans} 台×{FAN_KW}kW×{hrs}h，電價假設 NT${PRICE}/kWh）",
            priority=1 if lvl >= 2 else 2))
        # 這段高溫從什麼時候開始：同一段高溫內核准過一次，之後同樣的建議沿用該次核准（見 backend/loop.carry_over）
        decs[-1]["事件開始"] = episode_start(site["clean"]["thi"])
    # 解除條件：現場風扇開著、THI 已連續 2 小時低於 73.5（74 再減遲滯 0.5）→ 建議關閉
    thi2 = site["clean"]["thi"].iloc[-2:]
    if not fans and on_now and len(thi2) == 2 and bool((thi2 < 73.5).all()):
        decs.append(decision(name, "風扇-03／04／07／08", "關閉", "立即", f"0/{FAN_COUNT} 台",
                             f"THI 已連續 2 小時低於 73.5（現值 {thi:.1f}），現場仍有 {on_now} 台運轉",
                             f"每小時節省約 NT${on_now * FAN_KW * PRICE:.0f} 電費", "低", tr, False,
                             "THI 讀值不可信 → 維持運轉，待人工確認", priority=3))
    if 9 <= hour <= 16 and (temp or 0) >= 30:
        decs.append(decision(
            name, "南遮陽 25/26", "張開", "立即，至 16 時", "全幅",
            f"日照時段（{hour} 時）且舍溫 {temp:.1f}°C",
            (f"歷史回饋：遮陽張開時 THI 比同時段平常 {shade_eff['effect']:+.2f}（前後 2 小時變化量，n={shade_eff.get('n', 0)}，"
             + ("已驗證有效）" if shade_eff.get("verified") else "樣本不足或效果不顯著，屬試行）")
             if shade_eff.get("effect") is not None else "目前沒有遮陽的成效紀錄，屬試行；張開後追蹤 THI 變化"),
            "低", tr, bool(shade_eff.get("verified")), "遮陽馬達無回授 → 維持現狀並派員目視", priority=3))
    if rain and rain > 0:
        decs.append(decision(name, "南／北／西帆布", "張開（擋雨）", "立即", "全幅",
                             f"1 小時雨量 {rain} mm", "避免雨水打入、墊料潮濕", "中", tr, False,
                             "帆布馬達異常 → 人工手動操作", priority=2))
    else:
        decs.append(decision(name, "南／北／西帆布", "維持收起（通風）", "持續", "—",
                             "無降雨，帆布收起以利自然通風", "維持換氣量", "低", tr, False,
                             "遇雨或強風（風速>8 m/s）→ 人工確認後張開", priority=3, noop=True))
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
    if gap_exec:
        wos.append(work_order(name, "風扇自動連動設定", "生產",
                              f"建議開風扇 {replay['hours_need']} 小時、其中實際有開 {replay['hours_need_and_actual']} 小時；"
                              "將 THI≥79 連動風扇（試行期人工確認）", 3, due="3 天內"))
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

    # 三支 DO 當下都沒有讀值 → 主機斷線，不是探棒失效
    offline = bool(df[cfg["groups"]["DO"]].iloc[-1].isna().all())
    bad_do = [] if offline else probes["DO"]["排除"]
    bad_sc = [] if offline else probes["SC"]["排除"]
    if offline:
        status["等級"] = "無資料"
    ph1_n = int(site["reason"]["ph1"].str.contains("pH探棒不一致").sum())
    sc_day = c[["sc1", "sc2", "sc3"]].resample("D").mean().dropna(how="all")
    diags = []
    if offline:
        last = df[cfg["groups"]["DO"]].dropna(how="all").index
        diags.append(diag("主機斷線／無資料", "供電或通訊中斷",
                          f"三支 DO 目前都沒有讀值；最後有效讀值 {last[-1].strftime('%m/%d %H:%M') if len(last) else '—'}", "高"))
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
    gap_lv = mstop.get("level_gap_same_hour")
    if motor["n_stop_hours"] > 0 and gap_lv is not None and gap_lv < 0:
        diags.append(diag("停機時 DO 偏低", "日間停機排程（推測為節能）在低 DO 日仍執行",
                          f"至今停機 {motor['n_stop_hours']} 小時，集中於 {', '.join(str(h) for h in motor['peak_hours'])} 時；"
                          f"與前後 3 天同時段運轉相比，停機時共識 DO 低 {-gap_lv:.2f} mg/L（{mstop['n_days']} 天平均）", "中"))
    if pf < 0.85:
        diags.append(diag("功率因數偏低", "感應馬達負載、未設電容補償", f"近 7 日平均功率因數 {pf:.2f}", "高"))

    decs = []
    tr_do = status["感測可信度"]
    if lvl and lvl >= 1:
        decs.append(decision(
            name, "CPM 馬達（打氣／循環）", "保持運轉，暫停日間停機排程", "立即，至 DO 共識值連續 3 小時 ≥ 4.5",
            "100% 連續運轉", f"DO 共識值 {do:.2f} mg/L（{A.LEVEL_NAME[lvl]}，<4）；6 小時預測區間 "
            f"{_f(min(fc_do['lo']) if fc_do else None, '.2f')}–{_f(max(fc_do['hi']) if fc_do else None, '.2f')}",
            (f"歷史回饋：停機時 DO 比前後 3 天同時段運轉低 {-gap_lv:.2f}"
             + (f"±{mstop['se']:.2f}" if mstop.get("se") is not None else "")
             + f" mg/L（停機 {mstop['n']} 小時、{mstop['n_days']} 天，以日為單位檢定，"
             + ("已驗證）" if mstop["verified"] else f"未達驗證標準：需 ≥{MIN_SAMPLES} 小時、≥5 天且單尾 95% 顯著）")
             if gap_lv is not None else "目前還沒有停機紀錄可比較，依 DO 低於門檻採保守運轉"),
            "低", tr_do, mstop["verified"],
            "DO 探棒全部不可信 → 保守策略：馬達維持 24h 運轉並派員以手持式 DO 計量測",
            cost=f"多運轉約 {mstop['stop_hours_per_day']:.1f} h/日 × {float(df['power'].median())/1000:.2f} kW", priority=1))
    elif do is None:
        decs.append(decision(name, "CPM 馬達（打氣／循環）", "保守運轉 24 小時", "立即", "100%",
                             "主機斷線，目前沒有溶氧讀值" if offline else "三支 DO 探棒皆不可信，無法判斷溶氧",
                             "避免在無量測時缺氧", "低", 0, False,
                             "派員以手持式 DO 計量測", priority=1))
    else:
        # 解除條件：DO 共識值連續 3 小時 ≥ 4.5，且過去 24 小時曾低於 4（期間應已停用停機排程）→ 建議恢復排程
        good = probes["DO"]["可信探棒"]
        cons = c[good].median(axis=1) if good else pd.Series(dtype=float)
        recovered = len(cons) >= 3 and bool((cons.iloc[-3:] >= 4.5).all()) and bool((cons.iloc[-24:] < 4).any())
        if recovered:
            decs.append(decision(name, "CPM 馬達", "恢復日間停機排程（解除連續運轉）", "立即", "—",
                                 f"DO 共識值已連續 3 小時 ≥ 4.5（現值 {do:.2f}），過去 24 小時曾低於 4",
                                 "恢復節能排程", "中", tr_do, False, "恢復後 DO 再低於 4 → 重新連續運轉", priority=2))
        else:
            decs.append(decision(name, "CPM 馬達", "維持現行排程", "持續", "—", f"DO 共識值 {do:.2f} 正常",
                                 "—", "低", tr_do, False, "—", priority=3, noop=True))
    for k in bad_do:
        good = "／".join(x.upper() for x in probes["DO"]["可信探棒"]) or "無"
        decs.append(decision(name, f"{k.upper()} 探棒", f"自控制邏輯中排除，改用 {good} 表決", "立即", "—",
                             f"{k.upper()} 與另兩支不一致", "避免誤判缺氧而過度打氣／誤告警", "低",
                             _trust_now(site, k), False, "其餘探棒也失效 → 馬達 24h 運轉＋人工量測", priority=2))
    for k in bad_sc:
        decs.append(decision(name, f"{k.upper()} 探棒", "自控制邏輯中排除", "立即", "—", f"{k.upper()} 與另兩支 SC 不一致",
                             "SC 以其餘探棒中位數判斷", "低", _trust_now(site, k), False, "派工校正", priority=3))
    # 預測維護：停機模型可用且機率明顯偏高（≥50% 且 ≥ 歷史停機率 2 倍）→ 提前巡檢並開工單
    p_m, base = motor["p_now"], motor["base_rate"]
    model_ok = motor["method"] == "停機分類模型"
    high = model_ok and p_m is not None and p_m >= max(0.5, 2 * base)
    if high:
        decs.append(decision(name, "CPM 馬達", "提前巡檢（停機風險偏高）", "24 小時內", "—",
                             f"停機模型估未來 24 小時停機機率 {p_m:.0%}（歷史平均 {base:.0%}，模型驗證 AUC {motor['auc']:.2f}）；"
                             + _motor_trend(motor),
                             "在停機前發現異常（電流、軸承、葉輪、供電）", "低", _trust_now(site, "current"), False,
                             "模型不可用 → 依例行巡檢", priority=2))
    else:
        why = (motor["method"] if not model_ok else
               f"停機模型估未來 24 小時停機機率 {p_m:.0%}（歷史平均 {base:.0%}），未明顯偏高")
        decs.append(decision(name, "CPM 馬達", "依例行巡檢", "下次例行巡檢", "—", why + "；" + _motor_trend(motor),
                             "維持設備可用率", "低", _trust_now(site, "current"), False, "—", priority=3, noop=True))

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
    if high:
        wos.append(work_order(name, "CPM 馬達提前巡檢", "生產",
                              f"停機模型估 24h 停機機率 {p_m:.0%}（歷史 {base:.0%}）；檢查電流、軸承、葉輪與供電", 3,
                              "—", "24 小時內"))
    if pf < 0.85:
        wos.append(work_order(name, "功率因數改善評估", "能耗", f"平均 PF {pf:.2f}；評估加裝進相電容至 0.95", 1, "進相電容", "30 天內"))
    return {"status": status, "diagnoses": diags, "decisions": decs, "work_orders": wos,
            "forecast": [_fc_summary(fc_do, "DO1", "mg/L"), _fc_summary(fc_cur, "馬達電流", "A")],
            "equipment": {"馬達24h停機機率": None if motor["p_now"] is None else round(motor["p_now"], 3), "測試AUC": None if motor["auc"] is None else round(motor["auc"], 3),
                          "基準停機率": round(motor["base_rate"], 3),
                          "停機模型": motor.get("method"), "模型重訓次數": (motor.get("version") or {}).get("重訓次數"), "近7日用電kWh": None if kwh7 is None else round(kwh7, 1),
                          "平均功率因數": round(pf, 3)}}


def _motor_trend(motor):
    """馬達電流趨勢：近 7 日平均與至今平均比較（±10% 以內視為無明顯變化）。"""
    m7, ma = motor.get("cur_mean7d"), motor.get("cur_mean_all")
    if m7 is None or pd.isna(m7):
        return "近 7 日主機斷線或無電流資料，無法評估趨勢"
    if ma is None or pd.isna(ma) or ma == 0:
        return f"近 7 日平均電流 {m7:.2f} A"
    r = m7 / ma - 1
    trend = "無明顯變化" if abs(r) <= 0.1 else ("偏高，留意負載或軸承" if r > 0 else "偏低，留意葉輪或供電")
    return f"近 7 日平均電流 {m7:.2f} A，至今平均 {ma:.2f} A（{r:+.0%}，{trend}）"


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

    diags = []
    month = month.dropna()
    if len(month) >= 2 and month.iloc[-1] < 0.7 * month.iloc[0]:
        amp = float(diurnal.max() - diurnal.min()) if diurnal.notna().any() else 0.0
        cycle = (f"；日夜週期仍在（{int(diurnal.idxmax())} 時最高 {diurnal.max():.2f}、{int(diurnal.idxmin())} 時最低 {diurnal.min():.2f}，"
                 "符合光合作用特徵，支持①）" if amp >= 0.5 else "；日夜變化不明顯（幅度 <0.5），較支持②")
        diags.append(diag("DO 長期下降", "①真實有機污染負荷增加；②DO 探棒汙損老化（兩者並存可能）",
                          "月中位數 " + "→".join(f"{d.month}月 {v:.2f}" for d, v in month.items()) + cycle, "中"))
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
                             trust_do_eff, False, "探棒不可信 → 人工採樣", priority=2))
    else:
        decs.append(decision(name, "水質監測", "維持監測", "持續", "—", f"DO 近 48h 中位數 {do_med48:.2f} mg/L（未受污染）",
                             "—", "低", trust_do_eff, False, "探棒不可信 → 人工採樣", priority=3, noop=True))
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
    # 資料完整率：近 7 天有資料的小時比例低於 80% 才診斷（由資料計算，三站共用）
    recent = raw.loc[ts - pd.Timedelta(days=7):]
    comp7 = float(recent.notna().any(axis=1).mean()) if len(recent) else 0.0
    if comp7 < 0.8:
        boots = df["boots"].loc[ts - pd.Timedelta(days=7):].dropna() if "boots" in df else pd.Series(dtype=float)
        reboot = int(boots.max() - boots.min()) if len(boots) else 0
        csq = df["csq"].loc[ts - pd.Timedelta(days=7):].median() if "csq" in df else np.nan
        last_zero = bool((raw.iloc[-1].fillna(0) == 0).all())
        diags.append(diag("資料完整率過低", "供電中斷（太陽能／市電）或通訊中斷後未自動補傳",
                          f"近 7 天僅 {comp7:.1%} 的小時有資料；期間重開機 {reboot} 次"
                          + (f"；4G 訊號 CSQ 中位數 {csq:.0f}" + ("（良好）→ 較可能是供電問題" if csq >= 15 else "（偏弱）→ 也可能是通訊問題")
                             if pd.notna(csq) else ""), "高"))
        wos.append(work_order(name, "供電與資料補傳檢修", "數據品質",
                              f"近 7 天完整率 {comp7:.1%}、重開機 {reboot} 次" + ("、最後一筆全為 0" if last_zero else ""),
                              3, "電源模組／電池", "3 天內"))
    if used_peer:
        decs.append(decision(name, "本站空品判斷", f"改用鄰站（{used_peer}）數據", "立即", "—",
                             f"本站 PM2.5 可信度 {_f(trust_pm, '.0f')}，低於 {TRUST_FALLBACK}", "維持區域空品監測不中斷", "低",
                             trust_pm, False, f"以鄰站 {used_peer} 24h PM2.5 代表", priority=2))
    if used_peer:
        pass  # 本站已改走 Fallback（借用鄰站），不再重複產生監測決策
    elif lvl and lvl >= 2:
        # AQI「對敏感族群不健康」以上才發警示
        decs.append(decision(name, "空品告警", "發布廠區空品警示，減少戶外作業", "立即", "—",
                             f"PM2.5 24h 平均 {pm24:.1f}（AQI {aqi}，{A.LEVEL_NAME[lvl]}）", "降低人員暴露", "中", trust_pm, False,
                             "以鄰站數據判斷", priority=1))
    else:
        # AQI「普通」只提示，不要求核准
        decs.append(decision(name, "空品監測", "維持監測" + ("，敏感族群留意" if lvl == 1 else ""), "持續", "—",
                             f"PM2.5 24h 平均 {_f(pm24)} µg/m³（AQI {aqi}，{'普通' if lvl == 1 else '良好'}）", "—", "低",
                             trust_pm, False, "本站失效時以鄰站代表", priority=3, noop=True))
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
    # 噪音依時段比較管制值（營建工程噪音管制標準，假設為第二類管制區：日間 7–19 時 67、晚間 19–22 時 57、
    # 夜間 22–7 時 47 dB；夜間值待以環境部法規原文確認）
    hr = df.index.hour
    noise_lim = pd.Series(np.where((hr >= 7) & (hr < 19), 67, np.where((hr >= 19) & (hr < 22), 57, 47)), index=df.index)
    night = (hr >= 22) | (hr < 7)
    # 超過管制值 3 dB 以上才算（夜間背景音本身就接近 47 dB，差 1–2 dB 屬量測誤差範圍）
    loud_night = (df["noise"] > noise_lim + 3) & night
    heat = int((df["hazard"] > 0).sum())
    status = {"指數": "PM10（小時值，依新版 AQI 分級）", "數值": None if pm10 is None else round(pm10, 1),
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
    # 以下由「至今」資料計算，達條件才輸出
    humid_share = (over & rh_hi).sum() / max(1, over.sum())
    base_share = rh_hi.mean() if len(rh_hi) else 0
    if over.sum() >= 10 and humid_share >= max(0.5, base_share * 1.5):
        diags.append(diag("高濕時 PM 偏高", "光學式粉塵計在高濕度下吸濕高估，或逆溫擴散不良",
                          f"PM10 超過 {cfg['thresholds']['pm10'][0]} 的 {int(over.sum())} 小時中，{humid_share:.0%} 發生在濕度 ≥85%"
                          f"（全部時數中高濕只佔 {base_share:.0%}）", "中"))
    night_recent = int(loud_night.loc[ts - pd.Timedelta(days=7):].sum())
    if night_recent >= 3:
        diags.append(diag("夜間噪音超過管制值", "夜間施工或機具運轉",
                          f"近 7 天夜間（22–7 時）噪音超過 50 dB（管制值 47＋3）共 {night_recent} 小時，至今 {int(loud_night.sum())} 小時；"
                          f"夜間噪音中位數 {df['noise'][night].median():.1f} dB（背景音接近管制值）。"
                          "依營建工程噪音管制標準第二類管制區夜間值，管制區類別與夜間值待確認", "中"))
    decs = []
    if eta is not None or (lvl and lvl >= 1):
        decs.append(decision(name, "灑水／霧化降塵", "啟動", "立即" if lvl else f"{eta} 小時內", "周界全段",
                             f"PM10 現值 {_f(pm10, '.0f')}（{A.LEVEL_NAME[lvl or 0]}）"
                             + (f"，預測 {eta} 小時後超過 {cfg['thresholds']['pm10'][0]}" if eta and not lvl else ""), "抑制揚塵",
                             "低", tr, False, "粉塵計失效 → 依施工時段排程灑水", priority=1))
    elif (df["pm10"].iloc[-7:-2] >= cfg["thresholds"]["pm10"][0]).any() and bool(
            (df["pm10"].iloc[-2:] < 0.8 * cfg["thresholds"]["pm10"][0]).all()):
        # 解除條件：前幾小時曾超標、最近 2 小時已低於門檻的 80% → 建議停止灑水
        decs.append(decision(name, "灑水／霧化降塵", "停止", "立即", "—",
                             f"PM10 最近 2 小時低於 {0.8 * cfg['thresholds']['pm10'][0]:.0f}（現值 {_f(pm10, '.0f')}）",
                             "節省用水", "低", tr, False, "粉塵計失效 → 依施工時段排程", priority=2))
    else:
        decs.append(decision(name, "灑水／霧化降塵", "待命（不啟動）", "持續監測", "—",
                             f"PM10 現值 {_f(pm10, '.0f')}，6 小時內預測最高 {_f(_peak(fc, pm10), '.0f')}（<{cfg['thresholds']['pm10'][0]}）", "節省用水",
                             "低", tr, False, "粉塵計失效 → 依施工時段排程灑水", priority=3, noop=True))
    hz = df["hazard"].iloc[-1]
    if pd.notna(hz) and hz >= 2:   # 熱危害達 2 級才建議調整作業
        decs.append(decision(name, "作業調整（人工）", "暫停戶外重體力作業", "依熱指數即時判斷",
                             "每小時休息 15 分鐘起", f"熱危害等級 {int(hz)}（至今熱危害時數 {heat} 小時）", "預防熱傷害",
                             "高", tr, False, "熱指數失效 → 依氣象局高溫資訊", priority=1))
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
