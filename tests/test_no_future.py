"""「不偷看未來」測試：把某場域的資料截斷到時間 T（T 之後的資料完全不存在），重建整套系統，
在 T 的快照必須與完整資料版完全相同。相同就代表系統在 T 沒有用到任何 T 以後的資料。

涵蓋：每個場域多個時間點，包含第一次重訓前、剛好在重訓時間點、斷線期間。
執行：python -m tests.test_no_future        （約 10–20 分鐘；設定 OMP_NUM_THREADS=1 較快）
"""
import contextlib
import io
import json
import sys
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

import aiot.data as Dm                      # noqa: E402
from aiot import analysis as A               # noqa: E402
from backend import engine as Em             # noqa: E402

AIR = ("breed1", "breed1_north", "northeast")   # 三站互為鄰站，截斷時一起截
CASES = {
    "sheep": ["2026-07-12T10:00", "2026-07-15T00:00", "2026-07-25T12:00", "2026-08-08T15:00"],
    "expert": ["2026-06-10T09:00", "2026-06-24T03:00", "2026-07-10T09:00", "2026-08-31T00:00"],
    "river": ["2026-01-12T08:00", "2026-02-20T08:00", "2026-04-02T00:00"],
    "breed1": ["2026-06-05T12:00", "2026-06-29T15:00", "2026-07-03T15:00"],
    "breed1_north": ["2026-06-27T12:00", "2026-07-15T12:00"],
    "northeast": ["2026-07-20T10:00", "2026-08-23T20:00"],
    "site": ["2026-01-20T16:00", "2026-03-02T07:00", "2026-03-13T17:00"],
}


def build(cut_site=None, T=None):
    orig = Dm.load_site

    def cut(x):
        s = orig(x)
        same = x == cut_site or (cut_site in AIR and x in AIR)
        if T is not None and same:
            for k in ("df", "raw"):
                if k in s:
                    s[k] = s[k].loc[:T]
        return s

    Dm.load_site = cut
    try:
        for d in (A._ROLL, A._MOTOR_ROLL, A._FC_CACHE, A._MOTOR_CACHE):
            d.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            return Em.Engine()
    finally:
        Dm.load_site = orig


def main():
    full = build()
    ref = {(sid, t): json.dumps(full.snapshot(sid, t), ensure_ascii=False, sort_keys=True)
           for sid, ts in CASES.items() for t in ts}
    bad = 0
    for sid, ts in CASES.items():
        for t in ts:
            e = build(sid, pd.Timestamp(t))
            a = json.loads(ref[(sid, t)])
            b = e.snapshot(sid, t)
            b = json.loads(json.dumps(b, ensure_ascii=False, sort_keys=True))
            diffs = [k for k in a if k != "range" and a[k] != b.get(k)]
            print(f"{sid:13s} {t}  {'完全相同' if not diffs else '不同：' + ', '.join(diffs)}", flush=True)
            bad += bool(diffs)
    print("全部通過" if not bad else f"{bad} 個時間點不一致")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
