"""이체 멱등성 키 테스트 — 같은 요청이 재전송돼도 출금은 한 번만 일어나는가.

핵심은 동시성이다. 같은 키로 12건이 동시에 와도 출금은 정확히 1건이고 응답은 모두 같은
거래여야 한다. 그리고 그것이 **무엇 때문에** 성립하는지를 대조군으로 확인한다:

  - 키 기록을 끄면 같은 이체가 여러 번 출금된다 (음성 대조 — 테스트가 의미 있다는 근거)
  - 사전 조회를 전부 무력화해도 1건이다 (보장은 조회가 아니라 유니크 제약에서 나온다)
"""
import threading
from decimal import Decimal

import pytest

from app import idempotency, ledger, models

from .conftest import TestingSession, auth_header

AMOUNT = 30_000.0
CONCURRENT_REQUESTS = 12


@pytest.fixture(autouse=True)
def no_random_failure(monkeypatch):
    """이체 경로의 무작위 대외계 실패를 끈다 — 출금 건수를 정확히 세기 위해서다."""
    monkeypatch.setattr("app.routers.transactions.random", lambda: 0.99)


def _fund(client, sender, receiver, sender_balance):
    db = TestingSession()
    try:
        ledger.ensure_accounts(db, (sender, receiver))
        for account_id, balance in ((sender, sender_balance), (receiver, Decimal("0.00"))):
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


def _transaction_count(sender):
    db = TestingSession()
    try:
        return db.query(models.Transaction).filter(models.Transaction.account_from == sender).count()
    finally:
        db.close()


def _post(client, token, sender, receiver, amount=AMOUNT, key=None):
    headers = dict(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        "/transactions/transfer",
        json={"account_from": sender, "account_to": receiver, "amount": amount, "currency": "KRW"},
        headers=headers,
    )


def _fire_concurrently(client, token, sender, receiver, key):
    results = [None] * CONCURRENT_REQUESTS
    barrier = threading.Barrier(CONCURRENT_REQUESTS)

    def fire(i):
        barrier.wait()
        results[i] = _post(client, token, sender, receiver, key=key)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(CONCURRENT_REQUESTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(r is not None for r in results), "응답을 받지 못한 요청이 있다"
    return results


@pytest.fixture
def staff(client):
    return auth_header(client, "staff", "Staff1234!")


# ── 동시 재전송 ───────────────────────────────────────────────────────────────

def test_concurrent_same_key_debits_exactly_once(client, staff):
    sender, receiver = "ACC-IDEM-C1", "ACC-IDEM-C2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    results = _fire_concurrently(client, staff, sender, receiver, key="idem-concurrent")

    assert {r.status_code for r in results} == {201}
    assert len({r.json()["id"] for r in results}) == 1, "응답이 서로 다른 거래를 가리킨다"
    assert len({tuple(sorted(r.json().items())) for r in results}) == 1, "응답 본문이 서로 다르다"
    assert _transaction_count(sender) == 1
    assert _balance(sender) == Decimal("1000000.00") - Decimal(str(AMOUNT))
    assert _balance(receiver) == Decimal(str(AMOUNT))
    replayed = [r for r in results if r.headers.get("Idempotent-Replayed") == "true"]
    assert len(replayed) == CONCURRENT_REQUESTS - 1


def test_negative_control_without_key_record_debits_repeatedly(client, staff, monkeypatch):
    """키를 기록하지 않으면(멱등성 장치를 끄면) 같은 이체가 여러 번 출금된다.

    이 테스트가 초록이라는 것은 위 테스트의 동시 요청이 실제로 경합했다는 뜻이다.
    """
    monkeypatch.setattr(idempotency, "record", lambda *args, **kwargs: None)
    sender, receiver = "ACC-IDEM-N1", "ACC-IDEM-N2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    _fire_concurrently(client, staff, sender, receiver, key="idem-negative")

    assert _transaction_count(sender) == CONCURRENT_REQUESTS
    assert _balance(sender) == Decimal("1000000.00") - Decimal(str(AMOUNT)) * CONCURRENT_REQUESTS


def test_unique_constraint_alone_guarantees_single_debit(client, staff, monkeypatch):
    """사전 조회(잠금 전·잠금 후)를 모두 무력화해도 출금은 1건이다 — 보장은 유니크 제약에서 나온다.

    MySQL REPEATABLE READ에서는 트랜잭션 스냅샷 때문에 다른 요청이 방금 커밋한 키가 조회에 안 보일 수
    있다. 그 상황을 흉내낸다: 요청마다 처음 두 번의 조회는 "없음"을 돌려주고, 커밋 시 유니크 제약
    위반 후의 조회만 실제 결과를 본다.
    """
    real = idempotency.stored_result
    calls = threading.local()

    def blind_until_conflict(*args, **kwargs):
        calls.n = getattr(calls, "n", 0) + 1
        return None if calls.n <= 2 else real(*args, **kwargs)

    monkeypatch.setattr(idempotency, "stored_result", blind_until_conflict)
    sender, receiver = "ACC-IDEM-U1", "ACC-IDEM-U2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    results = _fire_concurrently(client, staff, sender, receiver, key="idem-unique")

    assert {r.status_code for r in results} == {201}
    assert len({r.json()["id"] for r in results}) == 1
    assert _transaction_count(sender) == 1
    assert _balance(sender) == Decimal("1000000.00") - Decimal(str(AMOUNT))


# ── 키 재사용과 확정 결과 ─────────────────────────────────────────────────────

def test_same_key_different_body_is_rejected(client, staff):
    sender, receiver = "ACC-IDEM-R1", "ACC-IDEM-R2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    first = _post(client, staff, sender, receiver, amount=AMOUNT, key="idem-reuse")
    second = _post(client, staff, sender, receiver, amount=AMOUNT * 2, key="idem-reuse")

    assert first.status_code == 201
    assert second.status_code == 409
    assert _transaction_count(sender) == 1
    assert _balance(sender) == Decimal("1000000.00") - Decimal(str(AMOUNT))


def test_amount_is_normalized_for_comparison(client, staff):
    """30000과 30000.0은 같은 요청이다 — 금액은 원장과 같은 십진 표현으로 비교한다."""
    sender, receiver = "ACC-IDEM-F1", "ACC-IDEM-F2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    first = _post(client, staff, sender, receiver, amount=30000, key="idem-norm")
    second = _post(client, staff, sender, receiver, amount=30000.0, key="idem-norm")

    assert second.status_code == 201 and second.json()["id"] == first.json()["id"]


def test_definite_failure_is_replayed(client, staff):
    """잔액 부족으로 거절된 이체도 확정된 결과다 — 나중에 잔액이 생겨도 같은 키는 같은 실패를 돌려준다.

    재시도로 결과가 바뀌면 클라이언트는 "처음 요청이 결국 성공했는가"를 알 수 없다.
    """
    sender, receiver = "ACC-IDEM-D1", "ACC-IDEM-D2"
    _fund(client, sender, receiver, Decimal("10000.00"))

    first = _post(client, staff, sender, receiver, key="idem-fail")
    _fund(client, sender, receiver, Decimal("1000000.00"))     # 잔액 충전 후 재시도
    second = _post(client, staff, sender, receiver, key="idem-fail")

    assert first.json()["reason"] == "insufficient balance"
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert _balance(sender) == Decimal("1000000.00")


def test_exception_rolls_back_the_key_so_retry_succeeds(client, staff, monkeypatch):
    """처리 중 예외로 롤백되면 키도 남지 않는다 — 같은 키로 재시도하면 이체가 실행된다."""
    sender, receiver = "ACC-IDEM-E1", "ACC-IDEM-E2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    def boom(*args, **kwargs):
        raise RuntimeError("원장 장애")

    with monkeypatch.context() as m:
        m.setattr(ledger, "apply_transfer", boom)
        with pytest.raises(RuntimeError):
            _post(client, staff, sender, receiver, key="idem-exc")

    assert _transaction_count(sender) == 0
    retry = _post(client, staff, sender, receiver, key="idem-exc")
    assert retry.status_code == 201 and retry.json()["status"] == "success"
    assert retry.headers.get("Idempotent-Replayed") is None
    assert _balance(sender) == Decimal("1000000.00") - Decimal(str(AMOUNT))


def test_keys_are_scoped_per_user(client, staff, admin_auth):
    sender, receiver = "ACC-IDEM-S1", "ACC-IDEM-S2"
    _fund(client, sender, receiver, Decimal("1000000.00"))

    a = _post(client, staff, sender, receiver, key="idem-shared")
    b = _post(client, admin_auth, sender, receiver, key="idem-shared")

    assert a.json()["id"] != b.json()["id"]
    assert _transaction_count(sender) == 2


def test_key_length_is_validated(client, staff):
    resp = _post(client, staff, "ACC-IDEM-L1", "ACC-IDEM-L2", key="k" * (idempotency.MAX_KEY_LENGTH + 1))
    assert resp.status_code == 422
