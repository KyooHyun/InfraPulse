"""FDS 알림 검토, 룰 관리, 운영 통계 테스트."""
from itertools import count

_recipients = count(1)


def _create_high_value_alert(client, staff_auth) -> None:
    """MEDIUM(40점) 이상이 되어 알림이 생기는 이체를 실행한다.

    200만 원 → HIGH_VALUE(20) + HIGH_VALUE_TOP(10), 처음 보는 수취인 → NEW_RECIPIENT(10) = 40점.
    알림은 MEDIUM 이상에서만 생기므로 금액 룰 하나로는 부족하다. 점수는 이체 전에 매기므로
    무작위 실패와 무관하게 알림이 생긴다.
    """
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-FDS1", "account_to": f"ACC-FDS-NEW{next(_recipients)}",
              "amount": 2_000_000, "currency": "KRW"},
        headers=staff_auth,
    )
    assert resp.status_code == 201, f"이체 실패: {resp.json()}"


def _get_detected_alerts(client, risk_auth) -> list:
    alerts = client.get("/fds/alerts", headers=risk_auth).json()
    return [a for a in alerts if a["status"] == "DETECTED"]


# ── 알림 목록 접근 제어 ────────────────────────────────────────────────────────

def test_list_alerts_as_risk_officer(client, risk_auth, staff_auth):
    _create_high_value_alert(client, staff_auth)
    resp = client.get("/fds/alerts", headers=risk_auth)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_alerts_forbidden_for_staff(client, staff_auth):
    resp = client.get("/fds/alerts", headers=staff_auth)
    assert resp.status_code == 403


def test_list_alerts_unauthenticated(client):
    resp = client.get("/fds/alerts")
    assert resp.status_code == 401


def test_list_alerts_pagination(client, risk_auth, staff_auth):
    """skip/limit 파라미터가 올바르게 동작한다."""
    _create_high_value_alert(client, staff_auth)
    resp_all = client.get("/fds/alerts?limit=500", headers=risk_auth)
    resp_one = client.get("/fds/alerts?limit=1", headers=risk_auth)
    assert resp_one.status_code == 200
    assert len(resp_one.json()) <= 1
    assert len(resp_all.json()) >= len(resp_one.json())


# ── 알림 검토 ─────────────────────────────────────────────────────────────────

def test_review_alert_approve(client, risk_auth, staff_auth):
    """RISK_OFFICER는 FDS 알림을 APPROVE(정상)로 처리할 수 있다."""
    _create_high_value_alert(client, staff_auth)
    detected = _get_detected_alerts(client, risk_auth)
    assert detected, "DETECTED 알림이 없습니다 — HIGH_VALUE 룰이 동작하지 않았을 수 있습니다"

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
    _create_high_value_alert(client, staff_auth)
    detected = _get_detected_alerts(client, risk_auth)
    assert detected, "DETECTED 알림이 없습니다"

    alert_id = detected[0]["id"]
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "REJECT", "comment": "이상거래 확정"},
        headers=risk_auth,
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "REJECT"


def test_review_alert_invalid_decision(client, risk_auth, staff_auth):
    """유효하지 않은 decision 값은 422를 반환한다."""
    _create_high_value_alert(client, staff_auth)
    detected = _get_detected_alerts(client, risk_auth)
    assert detected, "DETECTED 알림이 없습니다"

    alert_id = detected[0]["id"]
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "MAYBE"},
        headers=risk_auth,
    )
    assert resp.status_code == 422


def test_review_alert_already_processed(client, risk_auth, staff_auth):
    """이미 처리된 알림을 재검토하면 400을 반환한다."""
    _create_high_value_alert(client, staff_auth)
    detected = _get_detected_alerts(client, risk_auth)
    assert detected

    alert_id = detected[0]["id"]
    # 첫 번째 검토
    client.post(f"/fds/alerts/{alert_id}/review", json={"decision": "APPROVE"}, headers=risk_auth)
    # 두 번째 검토 시도 → 400
    resp = client.post(
        f"/fds/alerts/{alert_id}/review",
        json={"decision": "REJECT"},
        headers=risk_auth,
    )
    assert resp.status_code == 400


# ── 룰 관리 ───────────────────────────────────────────────────────────────────

def test_list_rules_as_admin(client, admin_auth):
    resp = client.get("/fds/rules", headers=admin_auth)
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) > 0
    condition_types = {r["condition_type"] for r in rules}
    # 거래 룰 6종 + 시스템 신호 3종이 모두 시드되어 있어야 한다
    assert condition_types == {
        "HIGH_VALUE", "HIGH_VALUE_TOP", "BALANCE_DRAIN", "DEST_EMPTY", "NEW_RECIPIENT", "VELOCITY",
        "FAILURE_RATE", "LOGIN_FAILURE", "LATENCY",
    }


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
        json={"threshold": high_value_rule["threshold"]},
        headers=admin_auth,
    )


# ── FDS 운영 통계 ─────────────────────────────────────────────────────────────

def test_stats_structure(client, risk_auth, staff_auth):
    """GET /fds/stats 응답이 정량 지표 필드를 모두 포함한다."""
    _create_high_value_alert(client, staff_auth)
    resp = client.get("/fds/stats", headers=risk_auth)
    assert resp.status_code == 200
    body = resp.json()

    required_fields = {
        "total_transactions", "failed_transactions", "failure_rate_pct",
        "total_alerts", "pending_review", "detection_rate_pct",
        "false_positive_rate_pct", "alerts_by_rule",
        "avg_risk_score", "high_risk_count", "high_risk_rate_pct",
        "str_total", "pending_compliance",
        "active_rule_count", "triggered_rule_types", "rule_coverage_pct",
    }
    assert required_fields <= body.keys()


def test_stats_detection_rate_increases(client, risk_auth, staff_auth):
    """고액 이체 후 탐지율(detection_rate_pct)이 0보다 커야 한다."""
    _create_high_value_alert(client, staff_auth)
    resp = client.get("/fds/stats", headers=risk_auth)
    body = resp.json()
    assert body["total_transactions"] > 0
    assert body["total_alerts"] > 0
    assert body["detection_rate_pct"] > 0.0


def test_stats_rule_coverage_after_high_value(client, risk_auth, staff_auth):
    """고액 이체 후 rule_coverage_pct가 0보다 커야 한다 (HIGH_VALUE 룰 트리거)."""
    _create_high_value_alert(client, staff_auth)
    resp = client.get("/fds/stats", headers=risk_auth)
    body = resp.json()
    assert body["rule_coverage_pct"] > 0.0
    assert "HIGH_VALUE" in body["alerts_by_rule"]


def test_stats_forbidden_for_staff(client, staff_auth):
    resp = client.get("/fds/stats", headers=staff_auth)
    assert resp.status_code == 403


def test_comparison_pending_until_remeasured(client, risk_auth):
    """철회된 비교 결과 대신 재측정 대기 상태를 200으로 알린다 (503은 장애로 집계된다)."""
    resp = client.get("/fds/comparison", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
