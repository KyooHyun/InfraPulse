"""FDS 임시조치(이체 보류) 테스트.

HIGH 등급 이체는 입금되지 않고 보류 계정에 묶인다. 담당자가 승인하면 입금, 취소하거나 시간이 지나면
출금 계좌로 돌아간다. 확인하는 것:

  - 보류 금액은 가용 잔액에서 빠진다 — 보류 중 동시 이체가 같은 돈을 다시 쓰지 못한다
  - 승인·취소가 동시에 와도 하나만 반영된다 — 그리고 그 보장이 상태 조건 UPDATE에서 나온다는 대조군(MySQL)
  - 같은 멱등성 키로 재요청하면 새 이체가 아니라 보류된 그 거래를 돌려준다
  - 보류·승인·취소·만료 뒤에도 대사(app/reconciliation.py)가 일치한다
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import holds, ledger, models
from app.reconciliation import reconcile

from .conftest import TEST_DB, TestingSession, auth_header

HIGH = (85.0, ["BALANCE_DRAIN"], {"BALANCE_DRAIN": 45.0})


@pytest.fixture(autouse=True)
def no_random_failure(monkeypatch):
    monkeypatch.setattr("app.routers.transactions.random", lambda: 0.99)


@pytest.fixture
def force_high(monkeypatch):
    """채점 결과를 HIGH로 고정한다 — 잔액을 마음대로 정한 계좌에서도 보류 경로를 타게 하려고."""
    monkeypatch.setattr("app.routers.transactions.evaluate_transaction", lambda *a, **k: HIGH)


@pytest.fixture
def staff(client):
    return auth_header(client, "staff", "Staff1234!")


@pytest.fixture
def risk(client):
    return auth_header(client, "risk_officer", "Risk1234!")


def _post(client, token, sender, receiver, amount, key=None):
    headers = dict(token)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/transactions/transfer",
        json={"account_from": sender, "account_to": receiver, "amount": amount, "currency": "KRW"},
        headers=headers,
    )


def _decide(client, token, tx_id, decision, comment="판단 근거"):
    return client.post(
        f"/transactions/{tx_id}/hold-decision", json={"decision": decision, "comment": comment}, headers=token,
    )


def _fund(sender, receiver, sender_balance):
    """계좌를 열고 출금 계좌 잔액을 맞춘다. 잔액 조정도 분개로 남긴다 — 대사가 계속 맞아야 하므로."""
    db = TestingSession()
    try:
        ledger.ensure_accounts(db, (sender, receiver))
        account = db.query(models.Account).filter(models.Account.account_id == sender).one()
        delta = Decimal(sender_balance) - Decimal(account.balance)
        account.balance = Decimal(sender_balance)
        ledger.post_journal(db, f"ADJ-{sender}", [(ledger.OPENING_EQUITY_ACCOUNT, -delta), (sender, delta)])
        db.commit()
    finally:
        db.close()


def _balance(account_id):
    db = TestingSession()
    try:
        return Decimal(db.query(models.Account.balance).filter(models.Account.account_id == account_id).scalar())
    finally:
        db.close()


def _journals(tx_id):
    db = TestingSession()
    try:
        rows = db.query(models.LedgerEntry).filter(models.LedgerEntry.transaction_id == tx_id).all()
        return sorted((e.journal_id, e.account_id, Decimal(e.amount)) for e in rows)
    finally:
        db.close()


def _assert_books_ok():
    db = TestingSession()
    try:
        report = reconcile(db)
    finally:
        db.close()
    assert report.ok, (report.unbalanced_journals, report.balance_mismatches, report.hold_mismatch)


def _audit_actions(tx_id):
    db = TestingSession()
    try:
        return [a for (a,) in db.query(models.AuditLog.action).filter(models.AuditLog.entity_id == str(tx_id))]
    finally:
        db.close()


# ── 보류 ──────────────────────────────────────────────────────────────────────

def test_high_risk_transfer_is_held_not_credited(client, staff):
    """잔액을 비우는 이체(룰 85점) → 202 held. 출금 계좌에서는 빠지고 입금 계좌에는 들어가지 않는다."""
    resp = _post(client, staff, "ACC-HOLD-A", "ACC-HOLD-A-NEW", 95_000_000)

    assert resp.status_code == 202, resp.text
    tx = resp.json()
    assert tx["status"] == "held" and tx["risk_score"] >= 70
    assert _balance("ACC-HOLD-A") == Decimal("5000000.00")
    assert _balance("ACC-HOLD-A-NEW") == ledger.DEFAULT_OPENING_BALANCE

    account = client.get("/transactions/accounts/ACC-HOLD-A", headers=staff).json()
    assert Decimal(str(account["balance"])) == Decimal("5000000")
    assert Decimal(str(account["held_amount"])) == Decimal("95000000")

    assert _journals(tx["id"]) == [
        (f"HOLD-{tx['id']}", "ACC-HOLD-A", Decimal("-95000000.00")),
        (f"HOLD-{tx['id']}", ledger.HOLD_ACCOUNT, Decimal("95000000.00")),
    ]
    _assert_books_ok()


def test_low_risk_transfer_is_not_held(client, staff):
    resp = _post(client, staff, "ACC-HOLD-LOW", "ACC-HOLD-LOW-R", 10_000)
    assert resp.status_code == 201 and resp.json()["status"] == "success"


def test_approve_releases_to_receiver(client, staff, risk):
    tx = _post(client, staff, "ACC-HOLD-B", "ACC-HOLD-B-NEW", 95_000_000).json()

    resp = _decide(client, risk, tx["id"], "APPROVE")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
    assert _balance("ACC-HOLD-B") == Decimal("5000000.00")
    assert _balance("ACC-HOLD-B-NEW") == ledger.DEFAULT_OPENING_BALANCE + Decimal("95000000")
    assert (f"RELEASE-{tx['id']}", "ACC-HOLD-B-NEW", Decimal("95000000.00")) in _journals(tx["id"])
    assert "RESOLVE_HOLD_APPROVE" in _audit_actions(tx["id"])
    _assert_books_ok()


def test_cancel_returns_to_sender(client, staff, risk):
    tx = _post(client, staff, "ACC-HOLD-C", "ACC-HOLD-C-NEW", 95_000_000).json()

    resp = _decide(client, risk, tx["id"], "CANCEL")

    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert _balance("ACC-HOLD-C") == ledger.DEFAULT_OPENING_BALANCE
    assert _balance("ACC-HOLD-C-NEW") == ledger.DEFAULT_OPENING_BALANCE
    assert "RESOLVE_HOLD_CANCEL" in _audit_actions(tx["id"])
    _assert_books_ok()


def test_decision_rules(client, staff, risk):
    """판단은 보류 상태에서 한 번만, 담당자만, 사유와 함께."""
    held = _post(client, staff, "ACC-HOLD-D", "ACC-HOLD-D-NEW", 95_000_000).json()
    normal = _post(client, staff, "ACC-HOLD-D2", "ACC-HOLD-D2-R", 10_000).json()

    assert _decide(client, staff, held["id"], "APPROVE").status_code == 403
    assert _decide(client, risk, held["id"], "APPROVE", comment="  ").status_code == 422
    assert _decide(client, risk, held["id"], "MAYBE").status_code == 422
    assert _decide(client, risk, normal["id"], "APPROVE").status_code == 409
    assert _decide(client, risk, 999_999, "APPROVE").status_code == 404

    assert held["id"] in [t["id"] for t in client.get("/transactions/held", headers=risk).json()]
    assert _decide(client, risk, held["id"], "CANCEL").status_code == 200
    assert _decide(client, risk, held["id"], "APPROVE").status_code == 409   # 이미 풀렸다
    assert held["id"] not in [t["id"] for t in client.get("/transactions/held", headers=risk).json()]
    _assert_books_ok()


def test_same_idempotency_key_returns_the_held_transfer(client, staff, risk):
    """재전송은 새 이체가 아니라 보류된 그 거래다. 풀린 뒤의 재전송은 풀린 상태를 돌려준다."""
    first = _post(client, staff, "ACC-HOLD-K", "ACC-HOLD-K-NEW", 95_000_000, key="hold-key-1")
    again = _post(client, staff, "ACC-HOLD-K", "ACC-HOLD-K-NEW", 95_000_000, key="hold-key-1")

    assert first.status_code == again.status_code == 202
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert again.json()["id"] == first.json()["id"]
    assert _balance("ACC-HOLD-K") == Decimal("5000000.00")   # 한 번만 빠졌다

    _decide(client, risk, first.json()["id"], "APPROVE")
    after = _post(client, staff, "ACC-HOLD-K", "ACC-HOLD-K-NEW", 95_000_000, key="hold-key-1")
    assert after.status_code == 201 and after.json()["status"] == "success"
    assert after.json()["id"] == first.json()["id"]
    _assert_books_ok()


# ── 동시성 ────────────────────────────────────────────────────────────────────

def test_held_funds_cannot_be_spent_by_concurrent_transfers(client, staff, force_high):
    """잔액 1억에서 3천만 원 이체 12건이 동시에 보류 판정을 받으면, 보류는 3건까지만 성립한다.

    보류 금액이 가용 잔액에서 바로 빠지므로 나머지는 잔액 부족으로 실패한다.
    """
    _fund("ACC-HOLD-CONC", "ACC-HOLD-CONC-R", Decimal("100000000.00"))
    results, barrier = [], threading.Barrier(12)

    def fire():
        barrier.wait()
        results.append(_post(client, staff, "ACC-HOLD-CONC", "ACC-HOLD-CONC-R", 30_000_000).json())

    threads = [threading.Thread(target=fire) for _ in range(12)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]

    held = [r for r in results if r["status"] == "held"]
    assert len(results) == 12
    assert len(held) == 3
    assert all(r["reason"] == "insufficient balance" for r in results if r["status"] != "held")
    assert _balance("ACC-HOLD-CONC") == Decimal("10000000.00")
    _assert_books_ok()


def _race_decisions(client, risk, tx_id, n=12):
    """승인 n/2건, 취소 n/2건을 동시에 보낸다. 상태 코드 목록을 돌려준다."""
    codes, barrier = [], threading.Barrier(n)

    def fire(decision):
        barrier.wait()
        codes.append(_decide(client, risk, tx_id, decision).status_code)

    threads = [threading.Thread(target=fire, args=("APPROVE" if i % 2 else "CANCEL",)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]
    return codes


def test_concurrent_approve_and_cancel_apply_exactly_once(client, staff, risk, monkeypatch):
    """승인 6건·취소 6건이 동시에 와도 반영은 1건. 상태를 바꾼 직후에 일부러 지연을 넣어도 같다 —
    보장이 타이밍이 아니라 `WHERE status = 'held'` 조건에서 나오기 때문이다."""
    tx = _post(client, staff, "ACC-HOLD-RACE", "ACC-HOLD-RACE-NEW", 95_000_000).json()
    real_claim = holds._claim

    def slow_claim(*args, **kwargs):
        won = real_claim(*args, **kwargs)
        time.sleep(0.1)
        return won

    monkeypatch.setattr(holds, "_claim", slow_claim)
    codes = _race_decisions(client, risk, tx["id"])

    assert sorted(codes) == [200] + [409] * 11
    sender, receiver = _balance("ACC-HOLD-RACE"), _balance("ACC-HOLD-RACE-NEW")
    approved = receiver == ledger.DEFAULT_OPENING_BALANCE + Decimal("95000000")
    cancelled = sender == ledger.DEFAULT_OPENING_BALANCE
    assert approved != cancelled   # 정확히 한쪽
    _assert_books_ok()


@pytest.mark.mysql
def test_negative_control_check_then_act_releases_twice_on_mysql(client, staff, risk, monkeypatch):
    """대조군: 상태를 먼저 읽고(잠금 없음) 나중에 쓰면, 동시 요청이 둘 다 'held'를 보고 둘 다 돈을 옮긴다.

    MySQL REPEATABLE READ의 일반 SELECT는 스냅샷을 읽으므로 잠금이 없다. 대사가 보류 계정 불일치로 잡아낸다.
    SQLite는 테스트에서 트랜잭션 전체를 직렬화(BEGIN IMMEDIATE)하므로 재현되지 않아 MySQL에서만 돈다.
    """
    tx = _post(client, staff, "ACC-HOLD-NAIVE", "ACC-HOLD-NAIVE-NEW", 95_000_000).json()

    def check_then_act(db, transaction_id, new_status, reason):
        current = db.query(models.Transaction.status).filter(models.Transaction.id == transaction_id).scalar()
        if current != holds.HELD:
            return False
        time.sleep(0.3)   # 읽기와 쓰기 사이 — 다른 요청도 'held'를 읽는다
        db.query(models.Transaction).filter(models.Transaction.id == transaction_id).update(
            {"status": new_status, "reason": reason}, synchronize_session=False)
        return True

    monkeypatch.setattr(holds, "_claim", check_then_act)
    codes = _race_decisions(client, risk, tx["id"], n=4)

    assert codes.count(200) > 1, codes   # 두 번 이상 풀렸다
    db = TestingSession()
    try:
        assert reconcile(db).hold_mismatch is not None
    finally:
        db.close()

    # 이 모듈의 DB는 뒤 테스트와 공유되므로 보정 분개로 장부를 맞춰 둔다
    _offset_hold_account()


def _offset_hold_account():
    """대조군의 이중 해제로 보류 계정에서 더 빠져나간 금액을 보정 분개로 메운다(테스트 정리용).

    이중 해제의 각 해제는 계좌 잔액과 분개를 함께 바꿨으므로 계좌 쪽은 맞고, 보류 계정만 어긋나 있다.
    """
    db = TestingSession()
    try:
        report = reconcile(db)
        assert report.balance_mismatches == []
        entries, held = report.hold_mismatch
        excess = held - entries
        db.add(models.LedgerEntry(journal_id="TEST-FIX", account_id=ledger.HOLD_ACCOUNT, amount=excess, currency="KRW"))
        db.add(models.LedgerEntry(journal_id="TEST-FIX", account_id=ledger.OPENING_EQUITY_ACCOUNT, amount=-excess,
                                  currency="KRW"))
        db.commit()
    finally:
        db.close()


# ── 만료 ──────────────────────────────────────────────────────────────────────

def test_expired_hold_is_cancelled_back_to_sender(client, staff, risk):
    tx = _post(client, staff, "ACC-HOLD-EXP", "ACC-HOLD-EXP-NEW", 95_000_000).json()
    db = TestingSession()
    try:
        assert tx["id"] not in holds.expire_holds(db, timedelta(hours=1))   # 방금 보류됨 — 아직 아니다
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        assert tx["id"] in holds.expire_holds(db, timedelta(hours=1), now=later)
    finally:
        db.close()

    assert _balance("ACC-HOLD-EXP") == ledger.DEFAULT_OPENING_BALANCE
    assert "EXPIRE_HOLD" in _audit_actions(tx["id"])
    assert _decide(client, risk, tx["id"], "APPROVE").status_code == 409
    _assert_books_ok()


def test_expiry_worker_runs_once_against_the_db(client, staff, monkeypatch):
    """docker-compose의 hold-expirer가 쓰는 진입점(app.hold_expiry.main)이 실제로 만료를 처리한다."""
    from app import hold_expiry

    tx = _post(client, staff, "ACC-HOLD-WRK", "ACC-HOLD-WRK-NEW", 95_000_000).json()
    # 보류 시각을 2시간 전으로 돌린다. "방금 보류된 것을 0시간으로 만료"로 시험하면 DB 서버(Docker VM)와
    # 테스트 프로세스의 시계 차이 때문에 MySQL에서 흔들린다.
    db = TestingSession()
    try:
        db.get(models.Transaction, tx["id"]).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(hold_expiry, "SessionLocal", TestingSession)

    assert hold_expiry.main(["--hours", "1"]) == 0

    assert _balance("ACC-HOLD-WRK") == ledger.DEFAULT_OPENING_BALANCE
    _assert_books_ok()
