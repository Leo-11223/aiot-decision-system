"""場域設定：感測器合理範圍、管制門檻、可控設備（動作目錄）與風險／成本。

所有門檻集中在這裡，決策引擎只讀設定，換場域或調整規則不必改程式。
"""

# ---------- 感測器物理合理範圍（超出 = 讀值不可信） ----------
# (min, max)；None 表示不檢查
VALID_RANGE = {
    "temp": (-5, 50), "rh": (5, 100), "pm25": (0, 500), "pm10": (0, 600),
    "hcho": (0, 1), "co": (0, 50), "tvoc": (0, 50), "wind": (0, 40), "noise": (30, 130),
    "co2": (300, 5000), "nh3": (0, 50), "ch4": (0, 50), "thi": (50, 100),
    "ph": (2, 12), "ec": (1, 5000), "do": (0, 20), "wtemp": (5, 40), "orp": (-500, 800),
    "sc": (0.5, 60), "level": (0.2, 5), "current": (0.05, 20), "power": (5, 3000),
    "pf": (0.5, 1.0), "heat_index": (0, 60), "battery": (0, 100), "batt_v": (6, 14),
}

# ---------- 空品分級（環境部 2025/1/1 起施行之新版 AQI）----------
# 出處：環境部空氣品質監測網「空氣品質指標」https://airtw.moenv.gov.tw/cht/Information/Standard/AirQualityIndicatorNew.aspx
# 三級切點 = 普通／對敏感族群不健康／對所有族群不健康 的下限
#   PM2.5（24h 平均）12.5／30.5／50.5 µg/m³；PM10（24h 平均）31／76／191 µg/m³；CO（8h 平均）4.5／9.5／12.5 ppm
# HCHO 0.08 ppm、TVOC 0.56 ppm 為「室內空氣品質標準」1 小時值，戶外站僅作參考；其後兩級為自訂。
AIR_THRESHOLDS = {"pm25": [12.5, 30.5, 50.5], "pm10": [31, 76, 191], "co": [4.5, 9.5, 12.5],
                  "hcho": [0.08, 0.1, 0.2], "tvoc": [0.56, 1.0, 3.0]}

SITES = {
    # ---------------------------------------------------------------- 羊舍
    "sheep": {
        "name": "羊舍（DO22340）", "file": "sheep.csv", "domain": "畜牧環控",
        "rename": {"ID4-溫度": "temp", "ID4-濕度": "rh", "ID4-CO2": "co2", "ID4-NH3": "nh3",
                   "ID4-CH4": "ch4", "成羊區平均溫度": "temp_avg", "成羊區平均濕度": "rh_avg",
                   "成羊區平均CO2": "co2_avg", "成羊區平均NH3": "nh3_avg", "成羊區平均CH4": "ch4_avg",
                   "成羊區THI": "thi", "仔羊區THI": "thi_lamb", "風速": "wind", "1小時雨量": "rain_1h"},
        "range_of": {"temp_avg": "temp", "rh_avg": "rh", "co2_avg": "co2", "nh3_avg": "nh3",
                     "ch4_avg": "ch4", "thi_lamb": "thi"},
        "key": ["thi", "thi_lamb", "temp", "rh", "co2", "nh3", "temp_avg", "nh3_avg"],
        "redundant": [("temp", "temp_avg", 3.0), ("nh3", "nh3_avg", 0.03)],
        "forecast": ["thi"],
        "anomaly": ["thi", "temp", "rh", "co2", "nh3"],
        # 畜舍 THI（NRC 1971）：<74 正常、74–78 警戒、79–83 危險、≥84 緊急
        "thresholds": {"thi": [74, 79, 84], "thi_lamb": [74, 79, 84], "co2": [1500, 2500, 3000],
                       "nh3": [10, 20, 25]},
        "devices": ["風扇-03", "風扇-04", "風扇-07", "風扇-08", "南遮陽張 25/26", "南右帆布張 23/24",
                    "南左帆布張 21/22", "北右帆布張 29/30", "北左帆布張 27/28", "西帆布張 31/32"],
    },
    # ---------------------------------------------------------------- 專家系統 C22（水產池）
    "expert": {
        "name": "專家系統 C22（水產池＋馬達）", "file": "expert.csv", "domain": "水質＋設備",
        "rename": {"pH1 即時值": "ph1", "pH2 即時值": "ph2", "pH3 即時值": "ph3",
                   "ORP1 即時值": "orp1", "ORP2 即時值": "orp2", "ORP3 即時值": "orp3",
                   "SC1 即時值": "sc1", "SC2 即時值": "sc2", "SC3 即時值": "sc3",
                   "DO1 即時值": "do1", "DO2 即時值": "do2", "DO3 即時值": "do3", "DO1水溫": "wtemp",
                   "OA4 溫度": "temp", "OA4 濕度": "rh", "OA4 熱指數": "heat_index",
                   "水位高度": "level", "CPM 平均電流": "current", "CPM 系統總和功率": "power",
                   "CPM 總和功率因數": "pf", "CPM 總用電度數": "kwh"},
        "range_of": {"ph1": "ph", "ph2": "ph", "ph3": "ph", "orp1": "orp", "orp2": "orp", "orp3": "orp",
                     "sc1": "sc", "sc2": "sc", "sc3": "sc", "do1": "do", "do2": "do", "do3": "do"},
        "key": ["do1", "do2", "do3", "ph1", "ph2", "ph3", "orp1", "orp2", "orp3", "sc1", "sc2", "sc3",
                "wtemp", "level", "current", "power", "pf", "temp", "heat_index"],
        # 三支同類探棒互為備援：與另兩支中位數差超過容許值 → 判定探棒可疑
        "groups": {"DO": ["do1", "do2", "do3"], "pH": ["ph1", "ph2", "ph3"],
                   "ORP": ["orp1", "orp2", "orp3"], "SC": ["sc1", "sc2", "sc3"]},
        "group_tol": {"DO": 1.5, "pH": 0.5, "ORP": 80, "SC": 4.0},
        "forecast": ["do1", "current"],
        "anomaly": ["do1", "do2", "ph2", "ph3", "orp1", "orp2", "orp3", "sc2", "sc3", "wtemp", "level", "current"],
        # 水產養殖 DO：<4 警戒、<3 危險、<2 緊急（越低越糟）
        "thresholds": {"do": [4, 3, 2], "ph_low": [6.8, 6.5, 6.0], "ph_high": [8.5, 8.8, 9.0]},
        "lower_is_bad": ["do"],
        "devices": ["CPM 馬達（打氣／循環）"],
    },
    # ---------------------------------------------------------------- 三爺宮溪
    "river": {
        "name": "仁德三爺宮溪（河川水質站）", "file": "river.csv", "domain": "河川水質",
        "rename": {"pH": "ph", "EC": "ec", "DO": "do", "水溫": "wtemp", "DO飽和度": "do_sat",
                   "電池電量": "battery", "電池電壓(V)": "batt_v", "太陽能電壓": "solar_v",
                   "pH斜率": "ph_slope", "EC斜率": "ec_slope", "DO斜率": "do_slope"},
        "key": ["do", "ph", "ec", "wtemp", "battery", "batt_v"],
        "forecast": ["do", "ec"],
        "anomaly": ["do", "ph", "ec", "wtemp"],
        # 河川污染指數 RPI 之 DO 分級：≥6.5 未受污染、4.6–6.5 輕度、2.0–4.5 中度、<2.0 嚴重
        "thresholds": {"do": [6.5, 4.6, 2.0], "ec": [750, 1500, 3000]},
        "lower_is_bad": ["do"],
        "devices": ["（無可控設備）告警／派工"],
    },
    # ---------------------------------------------------------------- 空品三站（同區互相比對）
    "breed1": {
        "name": "育成一廠（空品站）", "file": "breed1.csv", "domain": "廠區空品",
        "rename": {"風速": "wind", "TEMP": "temp", "RH": "rh", "PM2.5": "pm25", "HCHO": "hcho",
                   "CO": "co", "PM10": "pm10", "TVOC(PID)": "tvoc"},
        "key": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "forecast": ["pm25"],
        "anomaly": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "thresholds": AIR_THRESHOLDS,
        "devices": ["（無可控設備）告警／通報"],
    },
    "breed1_north": {
        "name": "育成一廠北側（空品站）", "file": "breed1_north.csv", "domain": "廠區空品",
        "rename": {"風速": "wind", "溫度": "temp", "濕度": "rh", "PM2.5": "pm25", "HCHO": "hcho",
                   "CO": "co", "TVOC PID": "tvoc", "PM10": "pm10", "電池電壓(V)": "batt_v",
                   "4G訊號(CSQ)": "csq", "開機次數(次)": "boots"},
        "key": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "forecast": [],
        "anomaly": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "thresholds": AIR_THRESHOLDS,
        "devices": ["（無可控設備）告警／通報"],
    },
    "northeast": {
        "name": "東北角（空品站）", "file": "northeast.csv", "domain": "廠區空品",
        "rename": {"風速": "wind", "TEMP": "temp", "RH": "rh", "PM2.5": "pm25", "HCHO": "hcho",
                   "CO": "co", "TVOC": "tvoc", "PM10": "pm10"},
        "key": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "forecast": ["pm25"],
        "anomaly": ["pm25", "pm10", "co", "hcho", "tvoc", "temp", "rh"],
        "thresholds": AIR_THRESHOLDS,
        "devices": ["（無可控設備）告警／通報"],
    },
    # ---------------------------------------------------------------- 工地微氣候
    "site": {
        "name": "工地微氣候（CB13505）", "file": "site.csv", "domain": "工地安全",
        "rename": {"風速": "wind", "溫度": "temp", "濕度": "rh", "熱指數": "heat_index",
                   "危害等級": "hazard", "PM2.5": "pm25", "PM10": "pm10", "噪音": "noise"},
        "key": ["pm25", "pm10", "noise", "temp", "rh", "wind"],
        "forecast": ["pm10"],
        "anomaly": ["pm25", "pm10", "noise", "temp", "rh"],
        # PM10／PM2.5：環境部 2025 新版 AQI 的「對敏感族群不健康／對所有族群不健康／非常不健康」下限。
        #   AQI 以 24h 平均計，工地這裡用小時值做即時預警（自訂作法，較保守）。
        # 噪音：營建工程噪音管制標準，假設為第二類管制區日間 Leq 67 dB（晚間 57、夜間 47 見 decision.build_site）。
        "thresholds": {"pm10": [76, 191, 355], "pm25": [30.5, 50.5, 125.5], "noise": [67]},
        "devices": ["灑水／霧化降塵", "作業調整（停工、休息）"],
    },
}

# 空品站互相比對：同一時間，同一物理量在三站差異過大 → 疑似單站感測器問題
AIR_PEERS = ["breed1", "breed1_north", "northeast"]
AIR_PEER_TOL = {"temp": 4.0, "rh": 20.0, "pm25": 25.0}

# ---------- 安全控制原則 ----------
TRUST_AUTO = 80      # 感測器可信度 ≥ 80 才允許自動執行
TRUST_FALLBACK = 50  # < 50：不信任此讀值，改走 Fallback（備援感測器／保守動作／派工）

# ---------- 執行回饋：動作升級為自動執行的門檻 ----------
MIN_SAMPLES = 30     # 至少 30 個實際執行的樣本
Z_ONE_SIDED = 1.645  # 單尾 95% 信心：效果 + 1.645 × 標準誤 仍 < 0 才算顯著
REVIEW_FLAG_MIN = 3  # 規則被審核至少 3 次，且駁回率 ≥ 50% → 建議檢討規則
