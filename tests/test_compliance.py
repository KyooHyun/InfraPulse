"""컴플라이언스 보고서(CTR/STR) 테스트."""


def _make_high_transfer(client, staff_auth, amount=15_000_000):
    """CTR 기준(1천만원) 초과 거래를 생성한다."""
    return client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-C001", "account_to": "ACC-C002", "amount": amount, "currency": "KRW"},
        headers=staff_auth,
    )


def test_ctr_auto_created_for_large_transaction(client, staff_auth, risk_auth):
    """1천만원 이상 거래 시 CTR이 자동 생성되어야 한다."""
    _make_high_transfer(client, staff_auth, amount=15_000_000)

    resp = client.get("/compliance/reports", headers=risk_auth)
    assert resp.status_code == 200
    reports = resp.json()
    ctr_reports = [r for r in reports if r["report_type"] == "CTR"]
    assert len(ctr_reports) > 0


def test_compliance_reports_have_report_number(client, risk_auth):
    """모든 보고서는 고유 보고서 번호를 가져야 한다."""
    resp = client.get("/compliance/reports", headers=risk_auth)
    assert resp.status_code == 200
    for report in resp.json():
        assert report["report_number"].startswith(("CTR-", "STR-"))


def test_list_reports_forbidden_for_staff(client, staff_auth):
    resp = client.get("/compliance/reports", headers=staff_auth)
    assert resp.status_code == 403


def test_submit_report(client, risk_auth, staff_auth):
    """PENDING 상태 보고서를 SUBMITTED로 전환할 수 있다."""
    _make_high_transfer(client, staff_auth, amount=20_000_000)
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    pending = [r for r in reports if r["status"] == "PENDING"]
    assert pending, "PENDING 보고서가 없습니다"

    report_id = pending[0]["id"]
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_submit_already_submitted_report(client, risk_auth, staff_auth):
    """이미 제출된 보고서를 재제출하면 400이어야 한다."""
    _make_high_transfer(client, staff_auth, amount=25_000_000)
    reports = client.get("/compliance/reports", headers=risk_auth).json()
    pending = [r for r in reports if r["status"] == "PENDING"]
    if not pending:
        return

    report_id = pending[0]["id"]
    client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    resp = client.post(f"/compliance/reports/{report_id}/submit", headers=risk_auth)
    assert resp.status_code == 400
