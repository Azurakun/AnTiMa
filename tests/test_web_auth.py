import time

import pytest

from utils import web_auth


def test_sign_roundtrip():
    assert web_auth.unsign(web_auth.sign("hello", 60)) == "hello"


def test_tampered_token_rejected():
    token = web_auth.make_user_cookie(123456789012345)
    raw = web_auth.base64.urlsafe_b64decode(token).decode()
    forged = raw.replace("123456789012345", "999999999999999", 1)
    assert web_auth.user_id_from_cookie(web_auth.base64.urlsafe_b64encode(forged.encode()).decode()) is None


def test_expired_token_rejected(monkeypatch):
    token = web_auth.sign("x", 10)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert web_auth.unsign(token) is None


@pytest.mark.parametrize("junk", [None, "", "not-base64!", "aGVsbG8="])
def test_garbage_rejected(junk):
    assert web_auth.unsign(junk) is None


def test_admin_cookie_requires_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    cookie = web_auth.make_admin_cookie()
    assert not web_auth.is_admin_cookie(cookie)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    assert web_auth.is_admin_cookie(cookie)
    assert not web_auth.is_admin_cookie(web_auth.make_user_cookie(1))


def test_check_admin_password(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse")
    assert web_auth.check_admin_password("correct horse")
    assert not web_auth.check_admin_password("wrong")
    assert not web_auth.check_admin_password("")


def test_user_cookie_is_not_admin(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    assert web_auth.user_id_from_cookie(web_auth.make_admin_cookie()) is None


def test_web_user_ids_stay_out_of_discord_range():
    for _ in range(100):
        uid = web_auth.new_web_user_id()
        assert web_auth.WEB_UID_MIN <= uid <= web_auth.WEB_UID_MAX
    assert web_auth.is_legacy_web_user_id("123456789012345")
    assert not web_auth.is_legacy_web_user_id("123456789012345678")  # Discord snowflake length
    assert not web_auth.is_legacy_web_user_id("abc")


@pytest.mark.parametrize("path,admin", [
    ("/", True),
    ("/api/details/rpgs", True),
    ("/api/action/restart", True),
    ("/api/rpg/manage/entity", True),
    ("/rpg/inspect/1", True),
    ("/ws", True),
    ("/chat", False),
    ("/play", False),
    ("/login", False),
    ("/healthz", False),
    ("/api/rpg/persona/save", False),
    ("/api/rpg/submit", False),
    ("/api/web_rpg/turn", False),
])
def test_admin_path_classification(path, admin):
    assert web_auth.is_admin_path(path) is admin
