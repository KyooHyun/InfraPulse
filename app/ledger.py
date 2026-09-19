"""계좌 원장 — 잔액 이동과 동시성 제어.

이 모듈이 존재하는 이유는 하나다. 이체는 "읽고 → 판단하고 → 쓰는" 연산인데,
그 사이에 다른 요청이 같은 계좌를 건드리면 잔액이 틀어진다. 잔액 10만원인 계좌에
8만원 이체 두 건이 동시에 들어오면, 둘 다 "잔액 10만원"을 읽고 둘 다 통과시켜
잔액이 -6만원이 되거나(초과 인출), 한쪽 차감이 다른 쪽 쓰기에 덮여 사라진다
(lost update). 애플리케이션 레벨 if 문으로는 막을 수 없다 — 두 요청 모두
자기 시점에서는 조건을 만족하기 때문이다.

해법은 DB에게 직렬화를 맡기는 것이다. `SELECT ... FOR UPDATE`로 계좌 행에
배타 잠금을 걸면, 먼저 잠근 트랜잭션이 커밋할 때까지 나머지는 그 행을 읽지 못하고
대기한다. 읽기와 쓰기가 같은 트랜잭션 안에 있는 한 중간 상태를 남에게 보이지 않는다.

주의 두 가지:

1) **잠금 순서** — 이체는 계좌 두 개를 잠근다. A→B 이체와 B→A 이체가 동시에
   들어와 각자 출금 계좌부터 잠그면 서로의 두 번째 계좌를 기다리며 데드락이 된다.
   그래서 출금/입금 구분 없이 **항상 account_id 오름차순으로** 잠근다. 모든
   트랜잭션이 같은 순서로 자원을 잡으면 순환 대기가 생기지 않는다.

2) **SQLite에서는 FOR UPDATE가 무시된다** — SQLAlchemy의 SQLite 방언은 이 구문을
   조용히 버린다(SQLite는 행 단위 잠금이 없고 DB 단위 쓰기 잠금만 있다). 운영
   DB인 MySQL에서는 실제로 행이 잠기고, 테스트에서 SQLite를 쓸 때는 BEGIN IMMEDIATE로
   같은 직렬화 효과를 얻는다. tests/conftest.py 의 `_sqlite_begin_immediate` 참고.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models

# 데모 시스템이라 계좌 개설 API가 따로 없다. 처음 등장한 계좌번호는 이 잔액으로 개설한다.
DEFAULT_OPENING_BALANCE = Decimal("100000000.00")

CENT = Decimal("0.01")


class InsufficientBalance(Exception):
    """출금 계좌 잔액이 이체 금액보다 적다."""

    def __init__(self, account_id: str, balance: Decimal, amount: Decimal):
        super().__init__(f"잔액 부족: {account_id} 잔액 {balance} < 요청 {amount}")
        self.account_id = account_id
        self.balance = balance
        self.amount = amount


def to_money(value: float | int | str | Decimal) -> Decimal:
    """float 금액을 원장용 십진수로 바꾼다.

    Decimal(float)가 아니라 Decimal(str(float))을 쓴다. 전자는 부동소수의 실제
    이진 값(0.1 → 0.1000000000000000055511151231257827…)을 그대로 가져온다.
    """
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def ensure_accounts(db: Session, account_ids: Iterable[str], currency: str = "KRW") -> None:
    """없는 계좌를 개설한다. **반드시 잠금 구간 밖에서** 먼저 호출한다.

    잠금 구간 안에서 INSERT를 하면, 두 요청이 같은 신규 계좌를 동시에 만들 때
    유니크 제약 위반이 잠금을 쥔 채 터져 롤백 범위가 이체 전체로 번진다.
    여기서 미리 만들어 두면 잠금 구간은 순수하게 UPDATE만 하게 된다.
    """
    for account_id in account_ids:
        exists = (
            db.query(models.Account.id)
            .filter(models.Account.account_id == account_id)
            .first()
        )
        if exists:
            continue
        db.add(
            models.Account(
                account_id=account_id,
                balance=DEFAULT_OPENING_BALANCE,
                currency=currency,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # 다른 요청이 한 발 먼저 같은 계좌를 만들었다 — 그쪽 결과를 그대로 쓴다.
            db.rollback()


def lock_accounts(db: Session, account_ids: Iterable[str]) -> Dict[str, models.Account]:
    """계좌 행에 배타 잠금(SELECT ... FOR UPDATE)을 걸고 돌려준다.

    데드락 방지를 위해 account_id 오름차순으로 한 건씩 잠근다. 한 번의 IN 쿼리로
    묶지 않는 이유는, 여러 행을 한 문장으로 잠글 때 DB가 잠그는 순서를 보장하지
    않기 때문이다(인덱스 스캔 순서에 따라 달라진다).
    """
    ordered: List[str] = sorted(set(account_ids))
    locked: Dict[str, models.Account] = {}
    for account_id in ordered:
        account = (
            db.query(models.Account)
            .filter(models.Account.account_id == account_id)
            .with_for_update()
            .one_or_none()
        )
        if account is None:
            raise LookupError(f"계좌를 찾을 수 없습니다: {account_id}")
        locked[account_id] = account
    return locked


def apply_transfer(
    sender: models.Account,
    receiver: models.Account,
    amount: Decimal,
) -> Dict[str, Decimal]:
    """잠긴 두 계좌 사이에서 잔액을 옮긴다. 커밋은 호출자가 한다.

    반환값은 거래 기록에 남길 이체 전후 잔액이다. 잔액이 부족하면
    InsufficientBalance를 올리고 어느 쪽 잔액도 건드리지 않는다.
    """
    sender_before = Decimal(sender.balance)
    receiver_before = Decimal(receiver.balance)

    if sender_before < amount:
        raise InsufficientBalance(sender.account_id, sender_before, amount)

    sender.balance = sender_before - amount
    receiver.balance = receiver_before + amount

    return {
        "balance_orig_before": sender_before,
        "balance_orig_after": Decimal(sender.balance),
        "balance_dest_before": receiver_before,
        "balance_dest_after": Decimal(receiver.balance),
    }


def total_balance(db: Session) -> Decimal:
    """전 계좌 잔액 합계 — 이체는 돈을 옮길 뿐이므로 이 값은 변하지 않아야 한다."""
    return sum(
        (Decimal(balance) for (balance,) in db.query(models.Account.balance).all()),
        Decimal("0"),
    )
