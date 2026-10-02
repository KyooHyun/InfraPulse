"""실제 MySQL에서만 확인할 수 있는 잠금 동작 — FOR UPDATE의 효과, 데드락, 잠금 대기 초과.

    FDS_TEST_DB=mysql pytest tests/test_mysql_locking.py

SQLite 테스트는 BEGIN IMMEDIATE로 DB 전체를 직렬화해 FOR UPDATE를 흉내냈다. 여기서는 InnoDB의 실제 행 잠금이
주장대로 동작하는지 본다. 기본 실행(SQLite)에서는 건너뛴다.
"""
import threading
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from app import ledger, models
from app.config import settings
from app.metrics import transfer_lock_retry_total

from .conftest import TEST_DB, TestingSession, auth_header

pytestmark = pytest.mark.mysql

OPENING = Decimal("100000.00")
AMOUNT = 30_000.0
CONCURRENT = 12


@pytest.fixture(autouse=True)
def no_random_failure(monkeypatch):
    monkeypatch.setattr("app.routers.transactions.random", lambda: 0.99)


@pytest.fixture
def staff(client):
    return auth_header(client, "staff", "Staff1234!")


def _fund(pairs):
    db = TestingSession()
    try:
        ledger.ensure_accounts(db, [a for a, _ in pairs])
        for account_id, balance in pairs:
            db.query(models.Account).filter(models.Account.account_id == account_id).one().balance = balance
        db.commit()
    finally:
        db.close()


def _balance(account_id):
    db = TestingSession()
    try:
        return Decimal(db.query(models.Account.balance).filter(models.Account.account_id == account_id).scalar())
    finally:
        db.close()


def _post(client, token, sender, receiver, key=None):
    headers = dict(token)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/transactions/transfer",
        json={"account_from": sender, "account_to": receiver, "amount": AMOUNT, "currency": "KRW"},
        headers=headers,
    )


def _retries(reason):
    return transfer_lock_retry_total.labels(reason=reason)._value.get()


def _lock_unsorted(db, account_ids, barrier=None):
    """ledger.lock_accounts에서 정렬만 뺀 것 — 받은 순서대로 잠근다(데드락 재현용)."""
    locked = {}
    for i, account_id in enumerate(account_ids):
        locked[account_id] = (
            db.query(models.Account).filter(models.Account.account_id == account_id).with_for_update().one()
        )
        if i == 0 and barrier is not None:
            barrier.wait(timeout=10)   # 두 트랜잭션이 각자 첫 계좌를 쥔 상태에서 만나게 한다
    return locked


def test_suite_is_running_on_mysql():
    assert TEST_DB == "mysql"


# ── FOR UPDATE의 효과 ─────────────────────────────────────────────────────────

def test_negative_control_without_for_update_mysql_overdraws(client, staff, monkeypatch):
    """FOR UPDATE를 빼면 실제 MySQL에서 초과 인출(lost update)이 일어난다.

    tests/test_concurrency.py가 같은 시나리오에서 통과하는 것이 잠금 덕분이라는 근거다.
    REPEATABLE READ의 일반 SELECT는 잠그지 않으므로 여러 요청이 같은 잔액을 읽고 각자 계산한 값을 쓴다.
    """
    def no_lock(db, account_ids):
        return {a: db.query(models.Account).filter(models.Account.account_id == a).one() for a in account_ids}

    monkeypatch.setattr(ledger, "lock_accounts", no_lock)
    sender, receiver = "ACC-MY-NOLOCK-S", "ACC-MY-NOLOCK-R"
    _fund([(sender, OPENING), (receiver, Decimal("0.00"))])

    results = [None] * CONCURRENT
    barrier = threading.Barrier(CONCURRENT)

    def fire(i):
        barrier.wait()
        results[i] = _post(client, staff, sender, receiver)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(CONCURRENT)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]

    succeeded = sum(1 for r in results if r is not None and r.status_code == 201 and r.json()["status"] == "success")
    debited = OPENING - _balance(sender)
    credited = _balance(receiver)
    broken = succeeded > 3 or debited != Decimal(str(AMOUNT)) * succeeded or credited != debited
    assert broken, f"잠금 없이도 정합성이 유지됐다 — 경합이 재현되지 않았다 (성공 {succeeded}, 출금 {debited})"


# ── 데드락 ────────────────────────────────────────────────────────────────────

def _cross_lock(first, second, lock):
    """두 트랜잭션이 (first→second), (second→first) 순서로 동시에 잠근다. 각자의 결과를 돌려준다."""
    barrier = threading.Barrier(2)
    outcome = {}

    def run(name, order):
        db = TestingSession()
        try:
            lock(db, order, barrier)
            db.commit()
            outcome[name] = "ok"
        except OperationalError as exc:
            db.rollback()
            outcome[name] = exc.orig.args[0]
        except threading.BrokenBarrierError:
            db.rollback()
            outcome[name] = "barrier"
        finally:
            db.close()

    threads = [
        threading.Thread(target=run, args=("t1", (first, second))),
        threading.Thread(target=run, args=("t2", (second, first))),
    ]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    return outcome


def test_unsorted_locking_deadlocks_on_mysql():
    """계좌 잠금 순서를 고정하지 않으면 A→B, B→A가 서로를 기다리고 MySQL이 데드락(1213)으로 하나를 죽인다."""
    _fund([("ACC-MY-DL-A", OPENING), ("ACC-MY-DL-B", OPENING)])

    outcome = _cross_lock("ACC-MY-DL-A", "ACC-MY-DL-B", _lock_unsorted)

    assert sorted(outcome.values(), key=str) == [1213, "ok"], outcome


def test_sorted_locking_does_not_deadlock():
    """같은 시나리오에서 ledger.lock_accounts(계좌번호 오름차순)는 데드락이 없다 — 순환 대기가 성립하지 않는다.

    정렬하면 두 트랜잭션이 같은 계좌를 먼저 잡으려 하므로, 하나가 첫 잠금에서 기다리고 장벽에 도달하지 못한다.
    그래서 장벽은 쓰지 않는다.
    """
    _fund([("ACC-MY-SO-A", OPENING), ("ACC-MY-SO-B", OPENING)])

    outcome = _cross_lock("ACC-MY-SO-A", "ACC-MY-SO-B", lambda db, ids, barrier: ledger.lock_accounts(db, ids))

    assert outcome == {"t1": "ok", "t2": "ok"}, outcome


def test_transfer_retries_after_real_deadlock(client, staff, monkeypatch):
    """이체 경로가 실제 데드락을 만나도 재시도로 두 이체가 모두 성공하고, 돈은 보존된다.

    잠금 순서를 일부러 깨서(출금 계좌부터) A→B, B→A를 동시에 보낸다. 첫 시도에서만 장벽으로 교차 대기를
    강제하고, 재시도는 그냥 잠근다. 희생된 트랜잭션은 MySQL이 롤백했으므로 재시도해도 이중 출금이 없다.
    """
    barrier = threading.Barrier(2)
    attempts = threading.local()

    def unsorted_once(db, account_ids):
        attempts.n = getattr(attempts, "n", 0) + 1
        return _lock_unsorted(db, account_ids, barrier if attempts.n == 1 else None)

    monkeypatch.setattr(ledger, "lock_accounts", unsorted_once)
    a, b = "ACC-MY-RT-A", "ACC-MY-RT-B"
    _fund([(a, OPENING), (b, OPENING)])
    before = _retries("deadlock")

    results = {}
    threads = [
        threading.Thread(target=lambda: results.__setitem__("ab", _post(client, staff, a, b))),
        threading.Thread(target=lambda: results.__setitem__("ba", _post(client, staff, b, a))),
    ]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]

    assert {r.status_code for r in results.values()} == {201}
    assert {r.json()["status"] for r in results.values()} == {"success"}
    assert _retries("deadlock") - before >= 1, "데드락이 재현되지 않았다"
    assert _balance(a) + _balance(b) == OPENING * 2
    assert _balance(a) == _balance(b) == OPENING   # 서로 같은 금액을 주고받았다


# ── 잠금 대기 초과 ─────────────────────────────────────────────────────────────

def test_lock_wait_timeout_returns_503_then_same_key_succeeds(client, staff, monkeypatch):
    """다른 트랜잭션이 계좌를 오래 쥐고 있으면 잠금 대기 초과(1205) → 재시도 → 503. 출금은 없다.

    잠금이 풀린 뒤 같은 Idempotency-Key로 다시 보내면 정상 처리된다(키는 실패한 시도에서 기록되지 않았다).
    """
    monkeypatch.setattr(settings, "transfer_lock_retries", 1)
    sender, receiver = "ACC-MY-TO-S", "ACC-MY-TO-R"
    _fund([(sender, OPENING), (receiver, Decimal("0.00"))])
    before = _retries("lock_wait_timeout")

    holder = TestingSession()
    try:
        holder.query(models.Account).filter(models.Account.account_id == sender).with_for_update().one()
        blocked = _post(client, staff, sender, receiver, key="my-timeout")
    finally:
        holder.rollback()
        holder.close()

    assert blocked.status_code == 503
    assert _retries("lock_wait_timeout") - before == 2   # 첫 시도 + 재시도 1회
    assert _balance(sender) == OPENING

    retry = _post(client, staff, sender, receiver, key="my-timeout")
    assert retry.status_code == 201 and retry.json()["status"] == "success"
    assert _balance(sender) == OPENING - Decimal(str(AMOUNT))
