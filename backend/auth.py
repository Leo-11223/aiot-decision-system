"""登入與權限。

· 密碼：PBKDF2-SHA256 加鹽雜湊，資料庫不存明碼。
· 登入憑證：伺服器簽章的 token（12 小時有效），前端放在 Authorization: Bearer 標頭。
· 角色：staff（員工：看資料、核准／駁回、更新工單）、admin（管理員：另可看操作紀錄、管理帳號）。
· 同一帳號＋同一來源 IP 連續輸錯 5 次鎖 5 分鐘；同一帳號 15 分鐘內累計輸錯 50 次（分散攻擊）也鎖 5 分鐘。
· 改密碼或重設密碼後，舊的登入憑證立即失效（token 內含密碼版本）。
· 初始管理員密碼檔 outputs/initial_admin.txt 在管理員第一次登入成功後自動刪除。

環境變數（部署時在雲端平台設定）：
  SECRET_KEY      token 簽章金鑰；未設定時每次啟動隨機產生（重啟後需重新登入）
  ADMIN_USERNAME  初始管理員帳號，預設 admin
  ADMIN_PASSWORD  初始管理員密碼；未設定時隨機產生，印在啟動訊息並存到 outputs/initial_admin.txt
  SEED_USERS      預先建立的帳號，格式「帳號,密碼,角色,顯示名稱;帳號,密碼,角色,顯示名稱」
                  （免費雲端主機重啟會清空資料庫，用這個讓員工帳號每次啟動自動建立）
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

ROLES = {"staff": "員工", "admin": "管理員"}
TOKEN_HOURS = 12
MAX_FAILS, LOCK_SECONDS = 5, 300
ACCOUNT_MAX_FAILS, ACCOUNT_WINDOW = 50, 900
ITERATIONS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

SECRET = (os.environ.get("SECRET_KEY") or secrets.token_hex(32)).encode()
_fails = {}            # (帳號, IP) → (次數, 鎖到何時)
_acct_fails = {}       # 帳號 → [失敗時間...]
_fails_lock = threading.Lock()
INITIAL_FILE = None    # bootstrap 設定：初始管理員密碼檔位置
_DUMMY_HASH = None     # 帳號不存在時也做一次雜湊比對，避免從回應時間猜出帳號是否存在


class AuthError(Exception):
    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


# ------------------------------------------------------------ 密碼
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), dk)
    except Exception:
        return False


def validate_password(pw: str):
    if len(pw) < 8:
        raise AuthError(422, "密碼至少 8 個字元")


def validate_username(name: str):
    if not USERNAME_RE.match(name or ""):
        raise AuthError(422, "帳號只能用英文字母、數字與 _ . -，長度 3 到 32")


# ------------------------------------------------------------ token
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pwv(password_hash: str) -> str:
    """密碼版本：密碼一改，舊 token 的版本就對不上而失效。"""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:12]


def make_token(username: str, password_hash: str = "") -> str:
    body = _b64(json.dumps({"u": username, "v": _pwv(password_hash),
                            "exp": int(time.time()) + TOKEN_HOURS * 3600}).encode())
    sig = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def user_from_token(store, token: Optional[str]) -> dict:
    if not token:
        raise AuthError(401, "請先登入")
    try:
        body, sig = token.split(".")
        expect = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            raise ValueError
        data = json.loads(_unb64(body))
    except Exception:
        raise AuthError(401, "登入狀態無效，請重新登入")
    if data.get("exp", 0) < time.time():
        raise AuthError(401, "登入已過期，請重新登入")
    u = store.get_user(data.get("u"))
    if not u or not u["active"]:
        raise AuthError(401, "帳號不存在或已停用")
    if not hmac.compare_digest(str(data.get("v", "")), _pwv(u["password_hash"])):
        raise AuthError(401, "密碼已變更，請重新登入")
    return public(u)


def public(u: dict) -> dict:
    return {"username": u["username"], "display_name": u["display_name"], "role": u["role"],
            "role_name": ROLES.get(u["role"], u["role"])}


# ------------------------------------------------------------ 登入
def login(store, username: str, password: str, ip: Optional[str] = None) -> dict:
    global _DUMMY_HASH
    name = (username or "")[:32]
    key = (name.lower(), ip or "-")
    now = time.time()
    with _fails_lock:
        n, until = _fails.get(key, (0, 0))
        recent = [x for x in _acct_fails.get(name.lower(), []) if x > now - ACCOUNT_WINDOW]
        _acct_fails[name.lower()] = recent
        if until > now:
            raise AuthError(429, f"密碼錯誤次數過多，請 {int(until - now) // 60 + 1} 分鐘後再試")
        if len(recent) >= ACCOUNT_MAX_FAILS and recent[-1] > now - LOCK_SECONDS:
            raise AuthError(429, "此帳號短時間內登入失敗次數過多，請 5 分鐘後再試")
    u = store.get_user(name) if USERNAME_RE.match(name) else None
    if u is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        check_password(password or "", _DUMMY_HASH)
    if not u or not u["active"] or not check_password(password or "", u["password_hash"]):
        with _fails_lock:
            n = _fails.get(key, (0, 0))[0] + 1
            _fails[key] = (n, now + LOCK_SECONDS if n >= MAX_FAILS else 0)
            _acct_fails.setdefault(name.lower(), []).append(now)
        shown = name if USERNAME_RE.match(name) else "（帳號格式不符）"
        store.log({"username": shown}, "login_failed", detail={"次數": n, "來源": ip})
        raise AuthError(401, "帳號或密碼錯誤")
    with _fails_lock:
        _fails.pop(key, None)
    store.update_user(name, last_login=store._now())
    pu = public(u)
    store.log(pu, "login", detail={"來源": ip})
    # 管理員第一次登入成功 → 刪除明碼的初始密碼檔
    if u["role"] == "admin" and INITIAL_FILE is not None and INITIAL_FILE.exists():
        try:
            INITIAL_FILE.unlink()
            store.log(pu, "initial_password_file_removed")
        except OSError:
            pass
    return {"token": make_token(name, u["password_hash"]), "user": pu, "expires_in": TOKEN_HOURS * 3600}


# ------------------------------------------------------------ 啟動時建立初始帳號
def bootstrap(store, out_dir: Path):
    global INITIAL_FILE
    INITIAL_FILE = out_dir / "initial_admin.txt"
    admin = os.environ.get("ADMIN_USERNAME", "admin")
    if not store.get_user(admin):
        pw = os.environ.get("ADMIN_PASSWORD")
        if not pw:
            pw = secrets.token_urlsafe(9)
            out_dir.mkdir(exist_ok=True)
            (out_dir / "initial_admin.txt").write_text(
                f"初始管理員帳號：{admin}\n密碼：{pw}\n登入後請到「我的帳號」修改密碼。\n"
                "（管理員第一次登入成功後，這個檔案會自動刪除）\n", encoding="utf-8")
            print(f"已建立初始管理員：帳號 {admin}，密碼 {pw}（也存在 outputs/initial_admin.txt）", flush=True)
        store.create_user(admin, "管理員", "admin", hash_password(pw))
    for item in filter(None, (os.environ.get("SEED_USERS") or "").split(";")):
        parts = [p.strip() for p in item.split(",")]
        if len(parts) < 3:
            continue
        name, pw, role = parts[:3]
        display = parts[3] if len(parts) > 3 else name
        if len(pw) < 8:
            print(f"SEED_USERS：帳號 {name} 的密碼少於 8 個字元，未建立", flush=True)
            continue
        if role in ROLES and USERNAME_RE.match(name) and not store.get_user(name):
            store.create_user(name, display, role, hash_password(pw))
