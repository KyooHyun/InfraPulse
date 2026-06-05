"""거래 API 테스트."""


def _transfer(client, headers, amount=10000, account_from="ACC-1111", account_to="ACC-2222"):
    return client.post(
        "/transactions/transfer",
        json={"account_from": account_from, "account_to": account_to, "amount": amount, "currency": "KRW"},
        headers=headers,
    )


def test_transfer_authenticated(client, staff_auth):
    resp = _transfer(client, staff_auth)
    assert resp.status_code == 201
    body = resp.json()
    assert body["amount"] == 10000
    assert body["currency"] == "KRW"
    assert "risk_score" in body
    assert body["status"] in ("success", "failed")


def test_transfer_unauthenticated(client):
    resp = _transfer(client, {})
    assert resp.status_code == 401


def test_transfer_negative_amount(client, staff_auth):
    resp = _transfer(client, staff_auth, amount=-5000)
    assert resp.status_code == 422


def test_transfer_zero_amount(client, staff_auth):
    resp = _transfer(client, staff_auth, amount=0)
    assert resp.status_code == 422


def test_transfer_same_account(client, staff_auth):
    resp = _transfer(client, staff_auth, account_from="ACC-9999", account_to="ACC-9999")
    assert resp.status_code == 422


def test_transfer_invalid_currency(client, staff_auth):
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-1", "account_to": "ACC-2", "amount": 1000, "currency": "BTC"},
        headers=staff_auth,
    )
    assert resp.status_code == 422


def test_transfer_high_value_creates_fds_alert(client, staff_auth, risk_auth):
    """100,000원 이상 거래는 HIGH_VALUE FDS 알림을 생성해야 한다."""
    _transfer(client, staff_auth, amount=200_000)

    alerts = client.get("/fds/alerts", headers=risk_auth).json()
    high_value_alerts = [a for a in alerts if a["alert_type"] == "HIGH_VALUE"]
    assert len(high_value_alerts) > 0


def test_list_transactions_authenticated(client, staff_auth):
    resp = client.get("/transactions", headers=staff_auth)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_transactions_unauthenticated(client):
    resp = client.get("/transactions")
    assert resp.status_code == 401
