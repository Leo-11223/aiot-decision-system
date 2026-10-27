"""預先訓練模型並存檔：python -m backend.warm

部署時在打包（docker build）階段執行一次，雲端主機啟動時直接讀取 outputs/engine.pkl。
存檔附有「版本指紋」：資料檔、程式碼或套件版本有變，指紋就不同，啟動時會自動改為重新訓練。
"""
import hashlib
import pickle
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKL = ROOT / "outputs" / "engine.pkl"


def fingerprint() -> str:
    import numpy, pandas, sklearn

    h = hashlib.sha1()
    # 只看會影響模型與決策的檔案：資料、分析程式、引擎（改登入、介面等不需重新訓練）
    files = sorted((ROOT / "data").glob("*.csv")) + sorted((ROOT / "aiot").glob("*.py")) + \
        [ROOT / "backend" / "engine.py", ROOT / "backend" / "warm.py"]
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    h.update(f"{sys.version_info[:2]}|{pandas.__version__}|{numpy.__version__}|{sklearn.__version__}".encode())
    return h.hexdigest()[:16]


def build_and_save():
    from .engine import Engine

    t = time.time()
    eng = Engine()
    PKL.parent.mkdir(exist_ok=True)
    with open(PKL, "wb") as f:
        pickle.dump({"fingerprint": fingerprint(), "engine": eng}, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"已存檔 {PKL.name}（{PKL.stat().st_size / 1e6:.1f} MB，訓練 {time.time() - t:.0f} 秒）", flush=True)
    return eng


def load_or_build():
    """有相符的存檔就讀檔，否則重新訓練（本機開發時通常走這條）。"""
    from .engine import Engine

    if PKL.exists():
        try:
            with open(PKL, "rb") as f:
                saved = pickle.load(f)
            if saved.get("fingerprint") == fingerprint():
                print("讀取預先訓練的模型存檔", flush=True)
                return saved["engine"]
            print("模型存檔已過期（資料或程式有更新），重新訓練", flush=True)
        except Exception as e:  # 存檔損壞或版本不相容
            print(f"無法讀取模型存檔（{e}），重新訓練", flush=True)
    return Engine()


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    build_and_save()
