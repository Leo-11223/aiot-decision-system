"""登入與權限。

· 密碼：PBKDF2-SHA256 加鹽雜湊，資料庫不存明碼。
· 登入憑證：伺服器簽章的 token（12 小時有效），前端放在 Authorization: Bearer 標頭。
· 角色：staff（員工：看資料、核准／駁回、更新工單）、admin（管理員：另可看操作紀錄、管理帳號）。
· 連續輸錯 5 次密碼，該帳號鎖 5 分鐘。

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
ITERATIONS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

SECRET = (os.environ.get("SECRET_KEY") or secrets.token_hex(32)).encode()
_fails = {}
_fails_lock = threading.Lock()


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


def make_token(username: str) -> str:
    body = _b64(json.dumps({"u": username, "exp": int(time.time()) + TOKEN_HOURS * 3600}).encode())
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
    return public(u)


def public(u: dict) -> dict:
    return {"username": u["username"], "display_name": u["display_name"], "role": u["role"],
            "role_name": ROLES.get(u["role"], u["role"])}


# ------------------------------------------------------------ 登入
def login(store, username: str, password: str) -> dict:
    key = (username or "").lower()
    with _fails_lock:
        n, until = _fails.get(key, (0, 0))
        if until > time.time():
            left = int(until - time.time()) // 60 + 1
            raise AuthError(429, f"密碼錯誤次數過多，請 {left} 分鐘後再試")
    u = store.get_user(username)
    if not u or not u["active"] or not check_password(password, u["password_hash"]):
        with _fails_lock:
            n = _fails.get(key, (0, 0))[0] + 1
            _fails[key] = (n, time.time() + LOCK_SECONDS if n >= MAX_FAILS else 0)
        store.log({"username": username}, "login_failed", detail={"次數": n})
        raise AuthError(401, "帳號或密碼錯誤")
    with _fails_lock:
        _fails.pop(key, None)
    store.update_user(username, last_login=store._now())
    pu = public(u)
    store.log(pu, "login")
    return {"token": make_token(username), "user": pu, "expires_in": TOKEN_HOURS * 3600}


# ------------------------------------------------------------ 啟動時建立初始帳號
def bootstrap(store, out_dir: Path):
    admin = os.environ.get("ADMIN_USERNAME", "admin")
    if not store.get_user(admin):
        pw = os.environ.get("ADMIN_PASSWORD")
        if not pw:
            pw = secrets.token_urlsafe(9)
            out_dir.mkdir(exist_ok=True)
            (out_dir / "initial_admin.txt").write_text(
                f"初始管理員帳號：{admin}\n密碼：{pw}\n登入後請到「我的帳號」修改密碼。\n", encoding="utf-8")
            print(f"已建立初始管理員：帳號 {admin}，密碼 {pw}（也存在 outputs/initial_admin.txt）", flush=True)
        store.create_user(admin, "管理員", "admin", hash_password(pw))
    for item in filter(None, (os.environ.get("SEED_USERS") or "").split(";")):
        parts = [p.strip() for p in item.split(",")]
        if len(parts) < 3:
            continue
        name, pw, role = parts[:3]
        display = parts[3] if len(parts) > 3 else name
        if role in ROLES and USERNAME_RE.match(name) and not store.get_user(name):
            store.create_user(name, display, role, hash_password(pw))
