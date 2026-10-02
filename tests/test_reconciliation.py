"""복식부기 엔트리와 원장 대사 — 잔액 컬럼이 엔트리 누적과 맞는가, 대사가 불일치를 잡아내는가."""
import threading
from decimal import Decimal

import pytest

from app import ledger, models
from app.reconciliation import reconcile

from .conftest import TestingSession, auth_header


@pytest.fixture(autouse=True)
def no_random_failure(monkeypatch):
    monkeypatch.setattr("app.routers.transactions.random", lambda: 0.99)


@pytest.fixture
def staff(client):
    return auth_header(client, "staff", "Staff1234!")


def _post(client, token, sender, receiver, amount):
    resp = client.post(
        "/transactions/transfer",
        json={"account_from": sender, "account_to": receiver, "amount": amount, "currency": "KRW"},
        headers=token,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _report():
    db = TestingSession()
    try:
        return reconcile(db)
    finally:
        db.close()


def _entries(transaction_id):
    db = TestingSession()
    try:
        return [
            (e.journal_id, e.account_id, Decimal(e.amount))
            for e in db.query(models.LedgerEntry).filter(models.LedgerEntry.transaction_id == transaction_id)
        ]
    finally:
        db.close()


def test_transfer_posts_one_debit_and_one_credit(client, staff):
    tx = _post(client, staff, "ACC-LED-A", "ACC-LED-B", 12_345.67)

    assert sorted(_entries(tx["id"])) == [
        (f"TX-{tx['id']}", "ACC-LED-A", Decimal("-12345.67")),
        (f"TX-{tx['id']}", "ACC-LED-B", Decimal("12345.67")),
    ]


def test_failed_transfer_posts_no_entries(client, staff):
    tx = _post(client, staff, "ACC-LED-POOR", "ACC-LED-B", 200_000_000)   # 개설 잔액(1억) 초과
    assert tx["reason"] == "insufficient balance"
    assert _entries(tx["id"]) == []


def test_books_reconcile_after_transfers(client, staff):
    for sender, receiver, amount in (("ACC-LED-C", "ACC-LED-D", 1_000_000), ("ACC-LED-D", "ACC-LED-E", 250_000.5),
                                     ("ACC-LED-E", "ACC-LED-C", 99.99)):
        _post(client, staff, sender, receiver, amount)

    report = _report()
    assert report.ok, (report.unbalanced_journals, report.balance_mismatches)
    assert report.accounts_checked > 0 and report.journals_checked > 0


def test_books_reconcile_after_concurrent_transfers(client, staff):
    """동시 이체 12건(잔액상 3건만 가능) 뒤에도 잔액과 엔트리가 맞는다 — 분개가 잔액 변경과 같은 커밋이라서다."""
    results = []
    barrier = threading.Barrier(12)

    def fire():
        barrier.wait()
        results.append(_post(client, staff, "ACC-LED-CONC-S", "ACC-LED-CONC-R", 30_000_000))

    threads = [threading.Thread(target=fire) for _ in range(12)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]

    assert sum(1 for r in results if r["status"] == "success") == 3
    report = _report()
    assert report.ok, (report.unbalanced_journals, report.balance_mismatches)


# ── 대사가 불일치를 잡아내는가 ────────────────────────────────────────────────

def test_reconciliation_catches_a_tampered_balance(client, staff):
    """분개 없이 잔액 컬럼만 바꾸면(직접 UPDATE, 버그, 위변조) 그 계좌가 정확한 차이와 함께 보고된다."""
    _post(client, staff, "ACC-LED-T1", "ACC-LED-T2", 50_000)
    db = TestingSession()
    try:
        account = db.query(models.Account).filter(models.Account.account_id == "ACC-LED-T1").one()
        original = Decimal(account.balance)
        account.balance = original + Decimal("1000.00")
        db.commit()

        report = reconcile(db)
        assert not report.ok
        assert report.balance_mismatches == [("ACC-LED-T1", original + Decimal("1000.00"), original)]
        assert report.unbalanced_journals == []
    finally:
        account.balance = original
        db.commit()
        db.close()
    assert _report().ok


def test_reconciliation_catches_a_missing_leg(client, staff):
    """분개의 한쪽 다리가 사라지면 그 분개의 합이 0이 아니게 되고, 해당 계좌의 잔액도 엔트리 누적과 어긋난다."""
    tx = _post(client, staff, "ACC-LED-M1", "ACC-LED-M2", 70_000)
    db = TestingSession()
    try:
        credit = (
            db.query(models.LedgerEntry)
            .filter(models.LedgerEntry.transaction_id == tx["id"], models.LedgerEntry.account_id == "ACC-LED-M2")
            .one()
        )
        saved = dict(journal_id=credit.journal_id, account_id=credit.account_id, amount=Decimal(credit.amount),
                     currency=credit.currency, transaction_id=credit.transaction_id)
        db.delete(credit)
        db.commit()

        report = reconcile(db)
        assert report.unbalanced_journals == [(f"TX-{tx['id']}", Decimal("-70000.00"))]
        assert [m[0] for m in report.balance_mismatches] == ["ACC-LED-M2"]
    finally:
        db.add(models.LedgerEntry(**saved))
        db.commit()
        db.close()
    assert _report().ok


def test_unbalanced_journal_is_rejected_at_write_time():
    db = TestingSession()
    try:
        with pytest.raises(ValueError):
            ledger.post_journal(db, "BAD-1", [("ACC-X", Decimal("-100")), ("ACC-Y", Decimal("90"))])
    finally:
        db.rollback()
        db.close()
