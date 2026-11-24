# utils/web_auth.py
"""
Authentication helpers for the web dashboard.

- Admin access: a password (DASHBOARD_PASSWORD) exchanged for a signed,
  expiring session cookie.
- Web players (chat / web RPG): a server-issued, signed user ID cookie, so a
  client can no longer claim an arbitrary user_id.

Cookies are signed with HMAC-SHA256 using DASHBOARD_SECRET_KEY. If that is not
set, a stable key is derived from the Discord token so sessions survive
restarts; set DASHBOARD_SECRET_KEY explicitly in production.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time

ADMIN_COOKIE = "atm_admin"
USER_COOKIE = "atm_uid"
ADMIN_SESSION_SECONDS = 12 * 60 * 60          # 12 hours
USER_SESSION_SECONDS = 365 * 24 * 60 * 60     # 1 year

# Web-only user IDs live in [1e14, 1e15) so they never collide with Discord snowflakes (17+ digits).
WEB_UID_MIN = 10**14
WEB_UID_MAX = 10**15 - 1


def _secret_key() -> bytes:
    key = os.getenv("DASHBOARD_SECRET_KEY")
    if key:
        return key.encode()
    discord_token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
    if discord_token:
        return hashlib.sha256(b"antima-dashboard:" + discord_token.encode()).digest()
    # Last resort: random per process (sessions reset on restart).
    global _EPHEMERAL_KEY
    if _EPHEMERAL_KEY is None:
        _EPHEMERAL_KEY = secrets.token_bytes(32)
    return _EPHEMERAL_KEY


_EPHEMERAL_KEY = None


def sign(value: str, max_age: int) -> str:
    """Returns 'value|expiry|signature' (base64-encoded)."""
    expires = int(time.time()) + max_age
    payload = f"{value}|{expires}"
    sig = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def unsign(token: str | None) -> str | None:
    """Returns the signed value if the token is authentic and unexpired, else None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        value, expires, sig = raw.rsplit("|", 2)
    except Exception:
        return None
    expected = hmac.new(_secret_key(), f"{value}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(expires) < time.time():
        return None
    return value


# --- Admin -----------------------------------------------------------------

def admin_password() -> str | None:
    pw = os.getenv("DASHBOARD_PASSWORD")
    return pw if pw else None


def check_admin_password(candidate: str) -> bool:
    pw = admin_password()
    if not pw or not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), pw.encode())


def make_admin_cookie() -> str:
    return sign("admin", ADMIN_SESSION_SECONDS)


def is_admin_cookie(token: str | None) -> bool:
    return admin_password() is not None and unsign(token) == "admin"


# --- Web users -------------------------------------------------------------

def make_user_cookie(user_id: int) -> str:
    return sign(f"uid:{user_id}", USER_SESSION_SECONDS)


def user_id_from_cookie(token: str | None) -> int | None:
    value = unsign(token)
    if not value or not value.startswith("uid:"):
        return None
    try:
        return int(value[4:])
    except ValueError:
        return None


def new_web_user_id() -> int:
    return WEB_UID_MIN + secrets.randbelow(WEB_UID_MAX - WEB_UID_MIN + 1)


def is_legacy_web_user_id(raw: str | None) -> bool:
    """Legacy IDs were generated client-side in the same 15-digit range."""
    if not raw or not raw.isdigit():
        return False
    return WEB_UID_MIN <= int(raw) <= WEB_UID_MAX


# --- Route classification --------------------------------------------------

# Paths that require an admin session. "/" is matched exactly; the rest by prefix.
ADMIN_EXACT = {"/"}
ADMIN_PREFIXES = (
    "/api/details/",
    "/api/history/",
    "/api/action/",
    "/api/control/",
    "/api/rpg/delete/",
    "/api/rpg/export/",
    "/api/rpg/memory/",
    "/api/rpg/debug/",
    "/api/rpg/manage/",
    "/rpg/inspect/",
    "/ws",
)


def is_admin_path(path: str) -> bool:
    return path in ADMIN_EXACT or path.startswith(ADMIN_PREFIXES)
