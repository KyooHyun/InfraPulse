"""컴플라이언스 보고서(STR) 테스트.

CTR은 현금 거래 대상이라 이체 경로에서는 생성되지 않는다.
"""
from app import models
from app.report_generator import create_str


def _transfer(client, staff_auth, amount):
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-C001", "account_to": "ACC-C002", "amount": amount, "currency": "KRW"},
        headers=staff_auth,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _make_str(client, staff_auth, db_session, amount=20_000_000) -> int:
    """이체를 하나 만들고 그 거래에 STR을 붙인다.

    룰 단독 구성에서는 HIGH(70점)에 도달하지 못하므로(calibration/reachability.py)
    API 경로로는 STR을 만들 수 없다. 보고서 처리 흐름만 검증하려고 생성 함수를 직접 부른다.
    """
    tx_id = _transfer(client, staff_auth, amount)["id"]
    tx = db_session.get(models.Transaction, tx_id)
    report_id = create_str(db_session, tx, reason="테스트용 고위험 거래").id
    # create_str의 refresh가 새 트랜잭션(BEGIN IMMEDIATE = 쓰기 잠금)을 연 채로 남는다.
    # 닫지 않으면 이어지는 API 호출이 잠금을 기다리다 실패한다.
    db_session.commit()
    return report_id


def test_transfer_does_not_create_ctr(client, staff_auth, risk_auth):
    """1천만원 이상 이체라도 CTR은 생기지 않는다 — CTR은 현금 거래 대상이다."""
    _transfer(client, staff_auth, 15_000_000)
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    assert [r for r in reports if r["report_type"] == "CTR"] == []


def test_compliance_reports_have_report_number(client, staff_auth, risk_auth, db_session):
    _make_str(client, staff_auth, db_session)
    resp = client.get("/compliance/reports", headers=risk_auth)
    assert resp.status_code == 200
    for report in resp.json():
        assert report["report_number"].startswith("STR-")


def test_list_reports_forbidden_for_staff(client, staff_auth):
    resp = client.get("/compliance/reports", headers=staff_auth)
    assert resp.status_code == 403


def test_submit_report(client, risk_auth, staff_auth, db_session):
    """PENDING 상태 보고서를 SUBMITTED로 전환할 수 있다."""
    report_id = _make_str(client, staff_auth, db_session)
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_submit_already_submitted_report(client, risk_auth, staff_auth, db_session):
    """이미 제출된 보고서를 재제출하면 400이어야 한다."""
    report_id = _make_str(client, staff_auth, db_session)
    client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 400
