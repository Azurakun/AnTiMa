import pytest
from fastapi.testclient import TestClient

import dashboard
from utils import web_auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    return TestClient(dashboard.app)


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_admin_api_requires_login(client):
    assert client.post("/api/action/restart").status_code == 401
    assert client.get("/api/details/rpgs").status_code == 401
    assert client.post("/api/rpg/delete/1").status_code == 401


def test_admin_page_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_flow(client):
    bad = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert bad.status_code == 303 and "error" in bad.headers["location"]
    assert web_auth.ADMIN_COOKIE not in client.cookies

    good = client.post("/login", data={"password": "s3cret"}, follow_redirects=False)
    assert good.status_code == 303 and good.headers["location"] == "/"
    assert web_auth.is_admin_cookie(client.cookies.get(web_auth.ADMIN_COOKIE))


def test_login_disabled_without_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    c = TestClient(dashboard.app)
    assert c.get("/login").status_code == 503
    # Even a validly signed cookie is useless without a configured password.
    c.cookies.set(web_auth.ADMIN_COOKIE, web_auth.make_admin_cookie())
    assert c.get("/api/details/rpgs").status_code == 401


def test_websocket_rejects_anonymous(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()


def test_web_endpoints_require_identity_cookie(client):
    # A client-supplied user_id no longer grants access to anyone's data.
    assert client.get("/api/chat/history?user_id=123456789012345").status_code == 401
    assert client.post("/api/web_rpg/rewind?thread_id=1&turn_id=1").status_code == 401


def test_identity_issues_cookie_and_claims_legacy_id(client, monkeypatch):
    claimed = set()

    def fake_claim(uid):
        if uid in claimed:
            return False
        claimed.add(uid)
        return True

    monkeypatch.setattr(dashboard, "_claim_web_id", fake_claim)

    r = client.post("/api/web/identity", json={"legacy_id": "123456789012345"})
    assert r.json() == {"user_id": "123456789012345"}
    assert web_auth.user_id_from_cookie(client.cookies.get(web_auth.USER_COOKIE)) == 123456789012345

    # A second browser cannot claim the same legacy ID; it gets a fresh one.
    other = TestClient(dashboard.app)
    r2 = other.post("/api/web/identity", json={"legacy_id": "123456789012345"})
    assert r2.json()["user_id"] != "123456789012345"
