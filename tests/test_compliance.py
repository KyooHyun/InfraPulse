"""컴플라이언스 보고서(STR) 테스트.

STR은 점수로 자동 제출되지 않는다 — 초안(DRAFT)을 담당자가 검토해 APPROVED가 된
것만 제출할 수 있다. CTR은 현금 거래 대상이라 이체 경로에서는 생성되지 않는다.
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


def _make_str_draft(client, staff_auth, db_session, amount=20_000_000) -> int:
    """이체를 하나 만들고 그 거래에 STR 초안을 붙인다.

    룰 단독 구성에서는 HIGH(70점)에 도달하지 못하므로(calibration/reachability.py)
    API 경로로는 STR을 만들 수 없다. 검토 흐름만 검증하려고 생성 함수를 직접 부른다.
    """
    tx_id = _transfer(client, staff_auth, amount)["id"]
    tx = db_session.get(models.Transaction, tx_id)
    report_id = create_str(db_session, tx, reason="테스트용 고위험 거래").id
    # create_str의 refresh가 새 트랜잭션(BEGIN IMMEDIATE = 쓰기 잠금)을 연 채로 남는다.
    # 닫지 않으면 이어지는 API 호출이 잠금을 기다리다 실패한다.
    db_session.commit()
    return report_id


def _review(client, auth, report_id, decision, comment="판단 근거"):
    return client.post(
        f"/compliance/reports/{report_id}/review",
        json={"decision": decision, "comment": comment},
        headers=auth,
    )


def test_transfer_does_not_create_ctr(client, staff_auth, risk_auth):
    """1천만원 이상 이체라도 CTR은 생기지 않는다 — CTR은 현금 거래 대상이다."""
    _transfer(client, staff_auth, 15_000_000)
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    assert [r for r in reports if r["report_type"] == "CTR"] == []


def test_str_is_created_as_draft(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    report = next(r for r in reports if r["id"] == report_id)
    assert report["status"] == "DRAFT"
    assert report["report_number"].startswith("STR-")


def test_list_reports_forbidden_for_staff(client, staff_auth):
    resp = client.get("/compliance/reports", headers=staff_auth)
    assert resp.status_code == 403


def test_draft_cannot_be_submitted_without_review(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 400


def test_approve_then_submit(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)

    resp = _review(client, risk_auth, report_id, "APPROVE", "단기간 다수 계좌 경유, 거래 목적 소명 불가")
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"

    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"

    # 재제출 불가
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 400


def test_dismissed_report_cannot_be_submitted(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    assert _review(client, risk_auth, report_id, "DISMISS", "급여 이체로 확인").json()["status"] == "DISMISSED"
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 400


def test_review_only_once(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    assert _review(client, risk_auth, report_id, "APPROVE").status_code == 200
    assert _review(client, risk_auth, report_id, "DISMISS").status_code == 400


def test_review_requires_comment(client, staff_auth, risk_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    assert _review(client, risk_auth, report_id, "APPROVE", "   ").status_code == 422


def test_review_is_audited_with_reviewer(client, staff_auth, risk_auth, admin_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    _review(client, risk_auth, report_id, "DISMISS", "가족 간 송금")

    entry = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.entity_type == "ComplianceReport",
                models.AuditLog.entity_id == str(report_id))
        .one()
    )
    assert entry.action == "REVIEW_STR_DISMISS"
    assert "가족 간 송금" in entry.detail
    assert entry.user_id is not None
    db_session.commit()


def test_staff_cannot_review(client, staff_auth, db_session):
    report_id = _make_str_draft(client, staff_auth, db_session)
    assert _review(client, staff_auth, report_id, "APPROVE").status_code == 403
