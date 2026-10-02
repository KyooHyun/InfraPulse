"""이체 멱등성 키 — 네트워크 재전송·클라이언트 재시도로 같은 이체가 두 번 출금되는 것을 막는다.

클라이언트는 이체마다 고유한 키를 `Idempotency-Key` 헤더로 보낸다. 같은 키가 다시 오면
이체를 다시 실행하지 않고 처음 결과(거래)를 그대로 돌려준다. 보장의 근거는 models.IdempotencyKey의
(user_id, key) 유니크 제약이고, 여기 있는 조회는 그 앞단의 최적화다.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy.orm import Session

from . import ledger, models, schemas

MAX_KEY_LENGTH = 64


class KeyReused(Exception):
    """같은 키로 내용(계좌·금액·통화)이 다른 요청이 왔다."""


def request_hash(req: schemas.TransferRequest) -> str:
    """요청 내용의 지문. 금액은 원장과 같은 십진 표현으로 정규화한다(1000과 1000.0은 같은 요청)."""
    canonical = "|".join((req.account_from, req.account_to, str(ledger.to_money(req.amount)), req.currency))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find(db: Session, user_id: int, key: str) -> Optional[models.IdempotencyKey]:
    return (
        db.query(models.IdempotencyKey)
        .filter(models.IdempotencyKey.user_id == user_id, models.IdempotencyKey.key == key)
        .one_or_none()
    )


def stored_result(db: Session, user_id: int, key: str, req_hash: str) -> Optional[models.Transaction]:
    """이미 처리된 키면 그때의 거래를 돌려준다. 처음 보는 키면 None.

    키는 같은데 내용이 다르면 KeyReused — 다른 이체에 키를 잘못 재사용한 것이므로, 처음 결과를
    돌려주면 클라이언트는 "새 이체가 성공했다"고 오해한다.
    """
    record = find(db, user_id, key)
    if record is None:
        return None
    if record.request_hash != req_hash:
        raise KeyReused(key)
    return db.get(models.Transaction, record.transaction_id)


def record(db: Session, user_id: int, key: str, req_hash: str, transaction: models.Transaction) -> None:
    """키를 거래와 같은 트랜잭션에 추가한다. 커밋은 호출자가 한다(잔액 변경과 함께)."""
    db.add(models.IdempotencyKey(
        user_id=user_id, key=key, request_hash=req_hash, transaction_id=transaction.id,
    ))
