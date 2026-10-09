"""Tài khoản web nội bộ: lưu trong data/users.json, mật khẩu băm PBKDF2. Sau thay bằng đăng nhập AD/LDAP."""
import hashlib
import hmac
import json
import secrets
import threading
import time

from .config import DATA_DIR, SESSION_HOURS, USERS_FILE

_ITER = 200_000
_sessions: dict[str, dict] = {}   # token -> {"user", "groups", "exp"}
_lock = threading.Lock()


def load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text(encoding="utf-8"))


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER).hex()


def set_user(username: str, password: str, groups: list[str]):
    if len(password) < 8:
        raise SystemExit("Mật khẩu tối thiểu 8 ký tự")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    users = load_users()
    salt = secrets.token_bytes(16)
    users[username] = {"salt": salt.hex(), "hash": _hash(password, salt), "groups": groups}
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=1), encoding="utf-8")


def login(username: str, password: str) -> str | None:
    u = load_users().get(username)
    if not u or not hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"]):
        time.sleep(0.5)  # làm chậm dò mật khẩu
        return None
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {"user": username, "groups": u["groups"], "exp": time.time() + SESSION_HOURS * 3600}
    return token


def session(token: str | None) -> dict | None:
    with _lock:
        s = _sessions.get(token or "")
        if s and s["exp"] > time.time():
            return s
        _sessions.pop(token or "", None)
    return None


def logout(token: str | None):
    with _lock:
        _sessions.pop(token or "", None)
