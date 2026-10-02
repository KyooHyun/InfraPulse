"""이체 동시성 테스트 — 같은 계좌에 동시 이체가 들어와도 잔액이 틀어지지 않는가.

이 파일이 검증하는 것은 셋이다.

  1) 잠금이 실제로 SQL에 실리는가 (FOR UPDATE 구문 확인)
  2) 잠금 순서가 항상 같은가 (데드락 방지)
  3) 동시 이체 후에도 잔액 항등식이 성립하는가 (초과 인출·lost update 없음)

(3)이 핵심이다. 잠금 없이 이 테스트를 돌리면 잔액이 음수가 되거나, 성공한 이체
건수와 줄어든 잔액이 맞지 않는다 — 두 요청이 같은 잔액을 읽고 각자 계산한 결과를
덮어쓰기 때문이다. 이 실패는 부하가 걸릴 때만 나타나고 로그에는 "정상 처리"로
남으므로, 테스트로 고정해 두지 않으면 배포 후에야 장부 불일치로 발견된다.

**이 테스트가 의미 있다는 근거** — 직렬화를 끄고 돌리면 실제로 깨진다:

    FDS_TEST_NO_LOCK=1 pytest tests/test_concurrency.py

    AssertionError: 잔액으로 감당 가능한 건수(3)보다 많이 성공했다: 11
    (마지막 숫자는 스케줄링에 따라 매번 달라진다)

12건 중 8건이 통과해 잔액 10만원에서 24만원이 빠져나간다. 항상 초록불인
동시성 테스트는 아무것도 증명하지 못하므로, 꺼서 빨간불이 되는지까지 확인한다.
"""
import threading
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql, sqlite

from app import ledger, models

from .conftest import TestingSession, auth_header, engine

# 초과 인출을 재현하려면 잔액이 요청 총액보다 적어야 한다.
OPENING = Decimal("100000.00")
TRANSFER_AMOUNT = Decimal("30000.00")   # 최대 3건까지만 성공 가능
CONCURRENT_REQUESTS = 12

SENDER = "ACC-CONC-SRC"
RECEIVER = "ACC-CONC-DST"


# ── 1) 잠금이 SQL에 실리는가 ──────────────────────────────────────────────────

def test_lock_query_emits_for_update(db_session):
    """MySQL 방언에서 with_for_update()가 실제 FOR UPDATE 구문으로 나간다."""
    statement = (
        db_session.query(models.Account)
        .filter(models.Account.account_id == SENDER)
        .with_for_update()
        .statement
    )
    assert "FOR UPDATE" in str(statement.compile(dialect=mysql.dialect())).upper()


def test_sqlite_silently_drops_for_update(db_session):
    """SQLite 방언은 FOR UPDATE를 버린다 — 테스트 DB의 한계를 문서가 아니라 코드로 고정한다.

    이 사실 때문에 conftest.py가 SQLite 연결에 BEGIN IMMEDIATE를 걸어 같은 직렬화
    효과를 만든다. 이 테스트가 깨진다면 SQLite가 행 잠금을 지원하게 됐다는 뜻이므로,
    그때는 conftest의 우회를 걷어내면 된다.
    """
    statement = (
        db_session.query(models.Account)
        .filter(models.Account.account_id == SENDER)
        .with_for_update()
        .statement
    )
    assert "FOR UPDATE" not in str(statement.compile(dialect=sqlite.dialect())).upper()


# ── 2) 잠금 순서 ──────────────────────────────────────────────────────────────

def test_accounts_are_locked_in_deterministic_order(client, db_session):
    """어느 방향의 이체든 계좌를 account_id 오름차순으로 잠근다.

    A→B와 B→A가 동시에 들어올 때 각자 출금 계좌부터 잠그면 서로의 두 번째 계좌를
    기다리며 데드락이 된다. 순서를 고정하면 순환 대기 자체가 성립하지 않는다.
    """
    ledger.ensure_accounts(db_session, ("ACC-ORDER-Z", "ACC-ORDER-A"))

    locked_order = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM accounts" not in statement or not parameters:
            return
        values = parameters if isinstance(parameters, (tuple, list)) else parameters.values()
        for value in values:
            if isinstance(value, str) and value.startswith("ACC-ORDER-"):
                locked_order.append(value)

    try:
        # 출금 계좌가 Z(사전순 뒤)인 방향으로 호출해도 A부터 잠겨야 한다.
        ledger.lock_accounts(db_session, ("ACC-ORDER-Z", "ACC-ORDER-A"))
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        db_session.rollback()

    assert locked_order == ["ACC-ORDER-A", "ACC-ORDER-Z"], (
        f"잠금 순서가 계좌번호 오름차순이 아니다: {locked_order}"
    )


# ── 3) 동시 이체 후 잔액 항등식 ───────────────────────────────────────────────

@pytest.fixture
def funded_accounts(client, db_session):
    """잔액이 빠듯한 출금 계좌와 수취 계좌를 만든다."""
    ledger.ensure_accounts(db_session, (SENDER, RECEIVER))
    for account_id, balance in ((SENDER, OPENING), (RECEIVER, Decimal("0.00"))):
        account = (
            db_session.query(models.Account)
            .filter(models.Account.account_id == account_id)
            .one()
        )
        account.balance = balance
    db_session.commit()
    return SENDER, RECEIVER


def _balance(account_id: str) -> Decimal:
    db = TestingSession()
    try:
        return Decimal(
            db.query(models.Account.balance)
            .filter(models.Account.account_id == account_id)
            .scalar()
        )
    finally:
        db.close()


def _fire_concurrent_transfers(client, token, sender, receiver, count):
    """count개의 이체를 같은 순간에 출발시키고 응답을 모은다."""
    results = [None] * count
    barrier = threading.Barrier(count)

    def fire(index: int) -> None:
        barrier.wait()   # 모든 스레드가 같은 순간에 출발해야 경합이 재현된다
        results[index] = client.post(
            "/transactions/transfer",
            json={
                "account_from": sender,
                "account_to": receiver,
                "amount": float(TRANSFER_AMOUNT),
                "currency": "KRW",
            },
            headers=token,
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return results


def test_concurrent_transfers_never_overdraw(client, funded_accounts):
    """잔액 10만원 계좌에 3만원 이체 12건을 동시에 던진다.

    성공은 최대 3건이어야 하고, 잔액은 음수가 될 수 없으며,
    줄어든 잔액은 성공 건수 × 이체금액과 정확히 일치해야 한다.
    """
    sender, receiver = funded_accounts
    token = auth_header(client, "staff", "Staff1234!")

    results = _fire_concurrent_transfers(client, token, sender, receiver, CONCURRENT_REQUESTS)

    assert all(response is not None for response in results), "응답을 받지 못한 요청이 있다"
    assert all(response.status_code == 201 for response in results), (
        [r.status_code for r in results]
    )

    bodies = [response.json() for response in results]
    succeeded = [b for b in bodies if b["status"] == "success"]
    rejected = [b for b in bodies if b["reason"] == "insufficient balance"]

    max_affordable = int(OPENING // TRANSFER_AMOUNT)   # 3건
    sender_balance = _balance(sender)
    receiver_balance = _balance(receiver)

    assert sender_balance >= 0, f"초과 인출이 발생했다: 잔액 {sender_balance}"
    assert len(succeeded) <= max_affordable, (
        f"잔액으로 감당 가능한 건수({max_affordable})보다 많이 성공했다: {len(succeeded)}"
    )
    assert sender_balance == OPENING - TRANSFER_AMOUNT * len(succeeded), (
        "성공 건수와 줄어든 잔액이 맞지 않는다 — lost update"
    )
    assert receiver_balance == TRANSFER_AMOUNT * len(succeeded), (
        "수취 계좌 입금액이 성공 건수와 맞지 않는다"
    )
    # 잔액이 바닥난 뒤의 요청은 무작위 실패가 아니라 잔액 부족으로 거절돼야 한다.
    assert rejected, "잔액 부족으로 거절된 요청이 하나도 없다 — 경합이 재현되지 않았다"


def test_money_is_conserved(client, funded_accounts):
    """이체는 돈을 옮길 뿐이므로 전 계좌 잔액 합계는 변하지 않는다."""
    sender, receiver = funded_accounts
    token = auth_header(client, "staff", "Staff1234!")

    probe = TestingSession()
    try:
        before = ledger.total_balance(probe)
    finally:
        probe.close()

    _fire_concurrent_transfers(client, token, sender, receiver, CONCURRENT_REQUESTS)

    probe = TestingSession()
    try:
        after = ledger.total_balance(probe)
    finally:
        probe.close()

    assert before == after, f"이체 과정에서 돈이 생기거나 사라졌다: {before} → {after}"


def test_insufficient_balance_is_rejected_not_crashed(client, db_session):
    """잔액을 넘는 이체는 201 + status=failed로 거절된다 (예외로 죽지 않는다)."""
    ledger.ensure_accounts(db_session, ("ACC-POOR", "ACC-RICH"))
    account = (
        db_session.query(models.Account)
        .filter(models.Account.account_id == "ACC-POOR")
        .one()
    )
    account.balance = Decimal("1000.00")
    db_session.commit()

    response = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-POOR", "account_to": "ACC-RICH",
              "amount": 999_999.0, "currency": "KRW"},
        headers=auth_header(client, "staff", "Staff1234!"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    # 무작위 실패가 먼저 걸렸을 수도 있으므로 둘 중 하나면 된다.
    assert body["reason"] in ("insufficient balance", "random failure")
    assert _balance("ACC-POOR") == Decimal("1000.00"), "거절된 이체가 잔액을 건드렸다"
