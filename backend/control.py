"""05 執行：把核准的決策轉成標準控制指令，送往「模擬控制器」並取得設備回報。

本專案沒有連到現場設備，所以這裡是**模擬**的 SCADA／PLC 介面：
  - 指令格式採工業常用的 Modbus TCP（功能碼 05 寫入單一線圈、06 寫入單一暫存器）。
  - 羊舍的線圈位址取自現場資料欄位名稱的點位編號（例如「南右帆布張 23/24」→ 張 23、收 24；「風扇-03」→ 3），
    專家系統馬達與工地灑水沒有點位資訊，位址為假設值（POINTS 中標示「假設」）。
  - 模擬控制器收到指令約 2 秒後回報「設備已執行」並回讀數值；可用「模擬通訊中斷」測試失敗流程：
    下發失敗 → 自動改派人工現場操作 → 人員回報完成。
正式導入時，只需把 SimController 換成企業現有 SCADA／PLC 的通訊模組，指令格式與流程不變。
"""
import re
import threading
from datetime import datetime, timedelta

from .store import TZ

ACK_SECONDS = 2   # 模擬控制器回報延遲

# ---------------------------------------------------------------- 點位表（模擬）
GATEWAYS = {
    "sheep": {"name": "羊舍環控 PLC", "unit": 1, "source": "線圈位址取自現場資料欄位的點位編號"},
    "expert": {"name": "C22 馬達控制器", "unit": 2, "source": "假設點位（現場資料無點位資訊）"},
    "site": {"name": "工地灑水控制器", "unit": 3, "source": "假設點位（現場資料無點位資訊）"},
}
FANS = [("風扇-03", 3), ("風扇-04", 4), ("風扇-07", 7), ("風扇-08", 8)]
CANVAS = [("南右帆布", 23, 24), ("南左帆布", 21, 22), ("北右帆布", 29, 30), ("北左帆布", 27, 28), ("西帆布", 31, 32)]
SHADE = ("南遮陽", 25, 26)


def _coil(addr, on, label):
    return {"功能碼": "05", "功能": "寫入單一線圈", "位址": f"{addr:05d}", "值": "ON" if on else "OFF", "點位": label}


def _reg(addr, value, label):
    return {"功能碼": "06", "功能": "寫入單一暫存器", "位址": str(addr), "值": str(value), "點位": label}


def _plan(kind, note, writes=None, site_id=None, undo=None):
    p = {"類型": kind, "說明": note}
    if writes:
        g = GATEWAYS[site_id]
        p.update({"協定": "Modbus TCP（模擬）", "控制器": g["name"], "站號": g["unit"], "點位來源": g["source"],
                  "寫入": writes})
        if undo:
            p["還原"] = undo
    return p


def undo_plan(p):
    """把一個控制計畫轉成它的還原計畫（撤回核准時用）。"""
    if not p.get("還原"):
        return None
    q = {k: v for k, v in p.items() if k not in ("寫入", "還原")}
    q["說明"] = f"撤回：{p['說明']}"
    q["寫入"] = p["還原"]
    return q


def plan(site_id: str, d: dict) -> dict:
    """決策 → 控制計畫。類型：設備指令／系統設定／系統通知／人工作業／維持現狀。
    設備指令同時附上「還原」寫入（撤回核准或目標達成時送出），確保有開就有關。"""
    dev, act = d.get("設備", ""), d.get("動作", "")
    if d.get("執行模式") == "維持現狀":
        return _plan("維持現狀", "維持目前狀態，不需下發")
    if site_id == "sheep" and dev.startswith("風扇"):
        if "關閉" in act:
            return _plan("設備指令", "關閉全部風扇", [_coil(a, False, name) for name, a in FANS], site_id,
                         undo=None)
        m = re.match(r"(\d+)\s*/", d.get("幅度", ""))
        n = int(m.group(1)) if m else len(FANS)
        writes = [_coil(a, i < n, name) for i, (name, a) in enumerate(FANS)]
        return _plan("設備指令", f"開啟 {n} 台風扇（其餘關閉）", writes, site_id,
                     undo=[_coil(a, False, name) for name, a in FANS])
    if site_id == "sheep" and dev.startswith("南遮陽"):
        name, open_, close = SHADE
        on = "張" in act
        return _plan("設備指令", f"{name}{'張開' if on else '收起'}（脈衝輸出）",
                     [_coil(open_ if on else close, True, f"{name}{'張' if on else '收'}")], site_id,
                     undo=[_coil(close if on else open_, True, f"{name}{'收' if on else '張'}")])
    if site_id == "sheep" and "帆布" in dev:
        on = "張" in act
        writes = [_coil(o if on else c, True, f"{name}{'張' if on else '收'}") for name, o, c in CANVAS]
        undo = [_coil(c if on else o, True, f"{name}{'收' if on else '張'}") for name, o, c in CANVAS]
        return _plan("設備指令", f"全部帆布{'張開' if on else '收起'}（脈衝輸出）", writes, site_id, undo=undo)
    if site_id == "expert" and dev.startswith("CPM 馬達"):
        if "恢復" in act:
            return _plan("設備指令", "恢復日間停機排程", [_reg(40010, 1, "日間停機排程（1=啟用）")], site_id,
                         undo=[_reg(40010, 0, "日間停機排程（0=停用）")])
        if "運轉" in act and "巡檢" not in act:
            return _plan("設備指令", "馬達持續運轉並停用日間停機排程",
                         [_coil(1, True, "馬達運轉"), _reg(40010, 0, "日間停機排程（0=停用）")], site_id,
                         undo=[_reg(40010, 1, "日間停機排程（1=啟用）")])
    if site_id == "site" and dev.startswith("灑水"):
        if "停止" in act:
            return _plan("設備指令", "關閉周界灑水閥", [_coil(1, False, "周界灑水閥")], site_id, undo=None)
        if "啟動" in act:
            return _plan("設備指令", "開啟周界灑水閥", [_coil(1, True, "周界灑水閥")], site_id,
                         undo=[_coil(1, False, "周界灑水閥")])
    if dev in ("告警規則", "本站空品判斷") or "排除" in act or "暫停" in act or "停用平均值" in act:
        return _plan("系統設定", "變更決策引擎的判斷設定，不送往現場設備")
    if dev.endswith("告警") and "發布" in act:
        return _plan("系統通知", "發布告警通知給現場人員，不送往現場設備")
    return _plan("人工作業", "需人員到現場執行（巡檢、採樣、校正、通報等），不下發控制指令；請以工單追蹤")


# ---------------------------------------------------------------- 模擬控制器
class SimController:
    """模擬控制器：記住每個場域是否「通訊中斷」，並依送出時間決定回報狀態。"""

    def __init__(self):
        self.offline = set()
        self.lock = threading.Lock()

    def set_offline(self, site_id, offline: bool):
        with self.lock:
            (self.offline.add if offline else self.offline.discard)(site_id)

    def is_offline(self, site_id):
        return site_id in self.offline

    def send(self, store, site_id, decision_id, data_time, d, p, operator, auto=False):
        """下發指令並寫入紀錄。通訊中斷 → 下發失敗，自動改派人工現場操作。"""
        failed = self.is_offline(site_id)
        status = "下發失敗" if failed else "已下發"
        note = ("控制器無回應（通訊中斷）→ 已改派人工現場操作" if failed else None)
        return store.add_command(site_id, decision_id, data_time, d.get("設備"), d.get("動作"), p,
                                 status, operator, note, auto)

    def refresh(self, store):
        """把送出超過 ACK_SECONDS 秒的「已下發」指令更新為「設備已執行」，並寫入回讀值。"""
        cutoff = (datetime.now(TZ) - timedelta(seconds=ACK_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
        for c in store.pending_commands(cutoff):
            readback = "；".join(f"{w['點位']} {w['位址']}＝{w['值']}" for w in c["plan"].get("寫入", []))
            store.update_command(c["id"], "設備已執行", None, f"設備回報：{readback}")


SIM = SimController()
