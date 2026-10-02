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


def _post(client, staff_auth, sender, recipient, amount):
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": sender, "account_to": recipient, "amount": amount, "currency": "KRW"},
        headers=staff_auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _alerts_for(client, risk_auth, transaction_id):
    alerts = client.get("/fds/alerts?limit=500", headers=risk_auth).json()
    return [a for a in alerts if a["transaction_id"] == transaction_id]


def test_medium_score_transfer_creates_fds_alert(client, staff_auth, risk_auth):
    """200만 원을 처음 보는 수취인에게 → 20 + 10 + 10 = 40점(MEDIUM) → 발화한 룰마다 알림."""
    tx = _post(client, staff_auth, "ACC-TX-M1", "ACC-TX-M-NEW", 2_000_000)

    assert tx["risk_score"] == 40.0
    types = {a["alert_type"] for a in _alerts_for(client, risk_auth, tx["id"])}
    assert types == {"HIGH_VALUE", "HIGH_VALUE_TOP", "NEW_RECIPIENT"}


def test_low_score_transfer_creates_no_alert(client, staff_auth, risk_auth):
    """LOW는 기록만 한다. 신규 수취인(10점) 하나만 발화한 소액 이체는 알림을 만들지 않는다."""
    tx = _post(client, staff_auth, "ACC-TX-L1", "ACC-TX-L-NEW", 10_000)

    assert tx["risk_score"] == 10.0
    assert _alerts_for(client, risk_auth, tx["id"]) == []


def test_balance_drain_reaches_high_and_drafts_str(client, staff_auth, risk_auth):
    """잔액을 거의 비우는 이체는 룰만으로 HIGH(70점)에 도달하고 STR 초안이 생긴다.

    새 계좌의 개설 잔액(1억)의 95%를 처음 보는 수취인에게 보낸다:
    BALANCE_DRAIN 45 + HIGH_VALUE 20 + HIGH_VALUE_TOP 10 + NEW_RECIPIENT 10 = 85점.
    예전 가중치로는 룰만으로 HIGH에 도달할 수 없어 이 경로가 죽어 있었다.
    """
    tx = _post(client, staff_auth, "ACC-TX-DRAIN", "ACC-TX-DRAIN-NEW", 95_000_000)

    assert tx["risk_score"] == 85.0
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    drafts = [r for r in reports if r["transaction_id"] == tx["id"]]
    assert len(drafts) == 1 and drafts[0]["status"] == "DRAFT"


def test_list_transactions_authenticated(client, staff_auth):
    resp = client.get("/transactions", headers=staff_auth)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_transactions_unauthenticated(client):
    resp = client.get("/transactions")
    assert resp.status_code == 401
