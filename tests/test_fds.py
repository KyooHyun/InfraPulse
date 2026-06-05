"""FDS 알림 검토 및 룰 관리 테스트."""


def _create_alert_via_transfer(client, staff_auth):
    """고액거래를 통해 FDS 알림을 생성한다."""
    client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-FDS1", "account_to": "ACC-FDS2", "amount": 500_000, "currency": "KRW"},
        headers=staff_auth,
    )


def test_list_alerts_as_risk_officer(client, risk_auth, staff_auth):
    _create_alert_via_transfer(client, staff_auth)
    resp = client.get("/fds/alerts", headers=risk_auth)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_alerts_forbidden_for_staff(client, staff_auth):
    resp = client.get("/fds/alerts", headers=staff_auth)
    assert resp.status_code == 403


def test_list_alerts_unauthenticated(client):
    resp = client.get("/fds/alerts")
    assert resp.status_code == 401


def test_review_alert_approve(client, risk_auth, staff_auth):
    """RISK_OFFICER는 FDS 알림을 APPROVE(정상)로 처리할 수 있다."""
    _create_alert_via_transfer(client, staff_auth)
    alerts = client.get("/fds/alerts", headers=risk_auth).json()
    detected = [a for a in alerts if a["status"] == "DETECTED"]
    assert detected, "검토할 DETECTED 알림이 없습니다"

    alert_id = detected[0]["id"]
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "APPROVE", "comment": "정상 거래 확인"},
        headers=risk_auth,
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "APPROVE"


def test_review_alert_reject(client, risk_auth, staff_auth):
    """RISK_OFFICER는 FDS 알림을 REJECT(이상거래 확정)로 처리할 수 있다."""
    _create_alert_via_transfer(client, staff_auth)
    alerts = client.get("/fds/alerts", headers=risk_auth).json()
    detected = [a for a in alerts if a["status"] == "DETECTED"]
    assert detected

    alert_id = detected[0]["id"]
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "REJECT", "comment": "이상거래 확정"},
        headers=risk_auth,
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "REJECT"


def test_review_alert_invalid_decision(client, risk_auth, staff_auth):
    _create_alert_via_transfer(client, staff_auth)
    alerts = client.get("/fds/alerts", headers=risk_auth).json()
    detected = [a for a in alerts if a["status"] == "DETECTED"]
    if not detected:
        return  # 알림이 없으면 스킵

    alert_id = detected[0]["id"]
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "MAYBE"},
        headers=risk_auth,
    )
    assert resp.status_code == 422


def test_list_rules_as_admin(client, admin_auth):
    resp = client.get("/fds/rules", headers=admin_auth)
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) > 0
    condition_types = [r["condition_type"] for r in rules]
    assert "HIGH_VALUE" in condition_types


def test_list_rules_forbidden_for_risk_officer(client, risk_auth):
    resp = client.get("/fds/rules", headers=risk_auth)
    assert resp.status_code == 403


def test_update_rule_threshold(client, admin_auth):
    """ADMIN은 FDS 룰 임계값을 운영 중 변경할 수 있다."""
    rules = client.get("/fds/rules", headers=admin_auth).json()
    high_value_rule = next(r for r in rules if r["condition_type"] == "HIGH_VALUE")

    resp = client.put(
        f"/fds/rules/{high_value_rule['id']}",
        json={"threshold": 200_000.0},
        headers=admin_auth,
    )
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 200_000.0

    # 원래 값으로 복구
    client.put(
        f"/fds/rules/{high_value_rule['id']}",
        json={"threshold": 100_000.0},
        headers=admin_auth,
    )
