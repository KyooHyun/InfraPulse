"""거래 기록의 금액이 저장 후에도 정확한가 — MySQL 단정밀도 FLOAT 버그의 회귀 테스트.

거래 금액·잔액 컬럼이 Float였을 때, MySQL에서는 단정밀도 FLOAT가 되어 유효숫자 6자리로만 돌아왔다 —
1,234,567원 → 1,234,570원, 12,345.67원 → 12,345.70원(SQLite는 8바이트 실수라 드러나지 않았다).
Float로 되돌리고 MySQL에서 돌리면 이 테스트가 실패하는 것을 확인했다. 두 DB 모두에서 돈다:

    pytest tests/test_money_precision.py                        # SQLite
    FDS_TEST_DB=mysql pytest tests/test_money_precision.py      # MySQL
"""
import pytest

from app import models

from .conftest import TestingSession

AMOUNTS = [12_345.67, 1_234_567.0, 123_456_789.12, 9_999_999_999.99, 0.01]


@pytest.mark.parametrize("amount", AMOUNTS)
def test_transaction_amount_round_trips_exactly(client, amount):
    db = TestingSession()
    try:
        tx = models.Transaction(
            account_from="ACC-PREC-A", account_to="ACC-PREC-B", amount=amount, currency="KRW",
            status="success", risk_score=0.0, balance_orig_before=amount, balance_dest_before=amount,
        )
        db.add(tx)
        db.commit()
        tx_id = tx.id
    finally:
        db.close()

    db = TestingSession()
    try:
        stored = db.get(models.Transaction, tx_id)
        assert stored.amount == amount
        assert stored.balance_orig_before == amount
    finally:
        db.close()



# ── API 경계 ──────────────────────────────────────────────────────────────────

def test_sub_cent_amount_is_rejected(client, staff_auth):
    """소수 둘째 자리를 넘는 금액은 거부한다 — 예전에는 float로 받아 원장 쪽에서 조용히 반올림했다."""
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-PREC-API1", "account_to": "ACC-PREC-API2", "amount": 100.005, "currency": "KRW"},
        headers=staff_auth,
    )
    assert resp.status_code == 422


def test_large_amount_is_exact_from_request_to_ledger_and_audit(client, staff_auth, admin_auth):
    """유효숫자 15자리 금액이 요청 → 거래 기록 → 응답 → 감사 로그까지 그대로 간다.

    잔액이 부족해 이체 자체는 거절되지만, 거절도 거래로 기록되므로 금액이 정확한지 볼 수 있다.
    """
    amount = 1_234_567_890_123.45
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-PREC-API3", "account_to": "ACC-PREC-API4", "amount": amount, "currency": "KRW"},
        headers=staff_auth,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["amount"] == amount

    db = TestingSession()
    try:
        assert db.get(models.Transaction, body["id"]).amount == amount
        entry = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.entity_type == "Transaction", models.AuditLog.entity_id == str(body["id"]))
            .one()
        )
        assert "amount=1234567890123.45" in entry.detail   # 예전에는 원 미만을 버린 형식(:,.0f)이었다
    finally:
        db.close()
