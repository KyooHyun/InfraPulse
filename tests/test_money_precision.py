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

