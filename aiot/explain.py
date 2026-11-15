"""白話說明：把快照轉成一般人看得懂的句子，回答命題的三個問題（現在／接下來／該做什麼）。
只用快照裡已有的資料組句，不另外推論。"""

MODE_TEXT = {
    "人工確認": "需要人工核准後才會執行",
    "自動執行": "低風險且效果已驗證，系統自動執行",
    "Fallback": "感測器讀值不可信，改用備案",
    "維持現狀": "不需動作",
}


def _num(v, unit=""):
    if v is None:
        return "—"
    v = float(v)
    s = f"{v:.0f}" if abs(v) >= 100 else (f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}")
    return s + (f" {unit}" if unit else "")


def _first_clause(text):
    for sep in ("；", "，", "。"):
        if sep in text:
            return text.split(sep)[0]
    return text


def decision_line(d):
    """單一決策的一句話：建議做什麼、為什麼、怎麼執行。"""
    amp = d.get("幅度") or "—"
    what = f"{d['設備']}{d['動作']}" + (f" {amp}" if amp not in ("—", "") else "")
    return f"建議{what}，因為{_first_clause(d.get('理由', ''))}；{MODE_TEXT.get(d.get('執行模式'), d.get('執行模式', ''))}。"


def site_summary(snap, cfg, indicator):
    """場域的三句話總結。indicator：(名稱, 單位, 門檻, 低值較差)。"""
    st = snap.get("status") or {}
    name, unit, cuts, lower_bad = indicator
    now_txt = f"{st.get('指數', name)} {_num(st.get('數值'))}，屬於「{st.get('等級', '—')}」"
    sensor_issues = [x for x in snap.get("diagnoses", []) if any(k in x["問題"] for k in
                     ("無反應", "階躍", "極端值", "飽和", "超出", "探棒", "尖峰", "完整率", "讀值無效"))]
    delay = st.get("資料延遲(h)")
    if delay is not None and delay >= 3:
        now_txt = f"已 {delay} 小時沒有新資料（感測器可能斷線），以下為最後讀值：" + now_txt
    if sensor_issues:
        now_txt += f"；另有 {len(sensor_issues)} 項感測器問題（已自動排除不可信的讀值）"

    nxt = "沒有預測項目"
    fc = next((f for f in snap.get("forecast", []) if f), None)
    if fc and fc.get("預測"):
        preds = [p for p in fc["預測"] if p is not None]
        cur = fc.get("現值")
        if preds and cur is not None:
            far = min(preds) if lower_bad else max(preds)
            trend = ("上升" if far > cur else "下降") if abs(far - cur) > 0.05 * max(abs(cur), 1) else "大致持平"
            nxt = f"未來 6 小時 {fc['變數']} 預估{trend}" + ("" if trend == "大致持平" else f"，最{'低' if lower_bad else '高'}約 {_num(far, fc.get('單位', ''))}")
            # 會不會跨過下一級門檻
            c = None
            for x in (cuts or []):
                crossed = (cur > x >= far) if lower_bad else (cur < x <= far)
                if crossed:
                    c = x
                    break
            if c is not None and fc["變數"] == name:   # 預測的就是狀態指標本身才比較門檻
                h = next((i + 1 for i, p in enumerate(preds) if (p <= c if lower_bad else p >= c)), None)
                nxt += f"，可能在 {h} 小時內{'低於' if lower_bad else '超過'} {c}"
            if "持續法" in fc.get("方法", ""):
                nxt += "（預測模型目前不夠準，以現值延續估計）"

    decs = [d for d in snap.get("decisions", []) if d.get("執行模式") != "維持現狀"]
    decs.sort(key=lambda d: d.get("優先", 9))
    if decs:
        d = decs[0]
        todo = f"優先處理「{d['設備']}{d['動作']}」，{MODE_TEXT.get(d['執行模式'], d['執行模式'])}"
        pending = sum(1 for x in decs if x["執行模式"] == "人工確認")
        if pending > 1 or (pending == 1 and d["執行模式"] != "人工確認"):
            todo += f"；共 {pending} 項決策等待核准"
    else:
        todo = "維持現狀，持續監測"
    return {"現在": now_txt, "接下來": nxt, "該做什麼": todo}
