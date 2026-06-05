"""인증 및 RBAC 테스트."""


def test_login_success(client):
    resp = client.post("/auth/token", data={"username": "admin", "password": "Admin1234!"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_login_wrong_password(client):
    resp = client.post("/auth/token", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/auth/token", data={"username": "ghost", "password": "any"})
    assert resp.status_code == 401


def test_get_me(client, admin_auth):
    resp = client.get("/auth/me", headers=admin_auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "ADMIN"


def test_get_me_without_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_login_failure_creates_fds_alert(client, risk_auth):
    """5분 내 3회 이상 로그인 실패 시 FDS 알림이 생성되어야 한다."""
    # 임계값(3회)을 초과하도록 4회 실패
    for _ in range(4):
        client.post("/auth/token", data={"username": "fds_test_user", "password": "wrong"})

    # FDS 알림 목록에 LOGIN_FAILURE 알림이 존재해야 한다
    resp = client.get("/fds/alerts", headers=risk_auth)
    assert resp.status_code == 200
    alert_types = [a["alert_type"] for a in resp.json()]
    assert "LOGIN_FAILURE" in alert_types
