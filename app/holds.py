"""FDS 임시조치 — 고위험 이체를 보류하고, 담당자 판단이나 시간 초과로 푼다.

통신사기피해환급법 제2조의5는 금융회사가 피해의심거래로 추정되는 계좌의 이체·송금을 지연하거나
일시 정지하도록 한다. 예전 이 시스템은 HIGH 등급이어도 이체를 실행하고 알림·STR 초안만 만들었다.

상태와 돈의 흐름 (ledger.HOLD_ACCOUNT = 보류 계정)

    이체 요청 ── 룰 점수 HIGH ──▶ held       출금 계좌 → 보류 계정     (분개 HOLD-<id>)
                                   │ 승인   ▶ success    보류 계정 → 입금 계좌 (분개 RELEASE-<id>)
                                   │ 취소   ▶ cancelled  보류 계정 → 출금 계좌 (분개 CANCEL-<id>)
                                   └ 만료   ▶ cancelled  보류 계정 → 출금 계좌 (분개 CANCEL-<id>)

- **가용 잔액**: 보류 금액은 보류 시점에 출금 계좌 잔액에서 빠진다. accounts.balance는 곧 가용 잔액이고,
  보류 중인 금액은 보류 계정에 있다. 그래서 보류 중에 들어온 다른 이체가 같은 돈을 쓸 수 없다.
- **한 번만 풀린다**: 해제는 `UPDATE ... WHERE id = ? AND status = 'held'`로 상태부터 바꾼다. 승인과 취소가
  동시에 와도 이 문장이 1행을 바꾸는 쪽은 하나뿐이고, 진 쪽은 0행을 보고 아무것도 옮기지 않는다.
  MySQL에서 UPDATE는 스냅샷이 아니라 최신 커밋 값을 읽어 WHERE를 다시 평가하므로 REPEATABLE READ에서도 성립한다.
  상태 변경·잔액 이동·분개는 한 커밋이다.
- **판단 근거는 룰 점수**다. ML 앙상블 점수는 커밋 뒤에 계산되므로(기본은 꺼져 있다) 보류 판단에 쓰지 않는다.

잠금 순서: 거래 행(UPDATE) → 계좌 행(오름차순). 이체 경로는 계좌 행만 잠그고 기존 거래 행을 잠그지 않으므로
순환 대기가 생기지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, ledger, models
from .metrics import transfer_hold_total

HELD = "held"
APPROVE = "APPROVE"
CANCEL = "CANCEL"

# 결정 → (새 상태, 사유, 분개 접두어, 메트릭 이벤트)
_OUTCOMES = {
    APPROVE: ("success", "hold approved", "RELEASE", "approved"),
    CANCEL: ("cancelled", "hold cancelled", "CANCEL", "cancelled"),
}


class NotHeld(Exception):
    """거래가 보류 상태가 아니다 — 이미 풀렸거나 보류된 적이 없다."""

    def __init__(self, transaction_id: int, status: Optional[str]):
        super().__init__(f"거래 {transaction_id}는 보류 상태가 아니다 (현재: {status})")
        self.transaction_id = transaction_id
        self.status = status


def _claim(db: Session, transaction_id: int, new_status: str, reason: str) -> bool:
    """held → new_status로 바꾼다. 이긴 요청만 True. 보장은 이 문장 하나에서 나온다."""
    changed = (
        db.query(models.Transaction)
        .filter(models.Transaction.id == transaction_id, models.Transaction.status == HELD)
        .update({"status": new_status, "reason": reason}, synchronize_session=False)
    )
    return changed == 1


def resolve(
    db: Session,
    transaction_id: int,
    decision: str,
    *,
    actor_id: Optional[int],
    comment: str,
    expired: bool = False,
) -> models.Transaction:
    """보류를 승인(APPROVE) 또는 취소(CANCEL)한다. 커밋까지 하고, 감사 로그를 남긴다.

    보류 상태가 아니면 NotHeld — 아무것도 바꾸지 않는다.
    """
    new_status, reason, journal_prefix, event = _OUTCOMES[decision]
    if expired:
        reason, event = "hold expired", "expired"

    try:
        if not _claim(db, transaction_id, new_status, reason):
            db.rollback()
            current = db.get(models.Transaction, transaction_id)
            raise NotHeld(transaction_id, current.status if current else None)

        tx = db.get(models.Transaction, transaction_id)
        db.refresh(tx)   # 위 UPDATE는 세션 객체를 갱신하지 않는다
        amount = ledger.to_money(tx.amount)
        accounts = ledger.lock_accounts(db, (tx.account_from, tx.account_to))
        target_id = tx.account_to if decision == APPROVE else tx.account_from
        target = accounts[target_id]
        if decision == APPROVE:
            # 돈이 실제로 입금 계좌에 들어가는 시점의 잔액으로 기록을 고친다.
            tx.balance_dest_before = float(target.balance)
        target.balance = Decimal(target.balance) + amount
        if decision == APPROVE:
            tx.balance_dest_after = float(target.balance)

        ledger.post_journal(db, f"{journal_prefix}-{tx.id}", [
            (ledger.HOLD_ACCOUNT, -amount),
            (target_id, amount),
        ], tx.currency, tx.id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(tx)
    transfer_hold_total.labels(event=event).inc()
    audit.log_event(
        db,
        action="EXPIRE_HOLD" if expired else f"RESOLVE_HOLD_{decision}",
        entity_type="Transaction",
        entity_id=str(tx.id),
        detail=f"amount={amount}, status={tx.status}, comment={comment}",
        user_id=actor_id,
    )
    return tx


def list_held(db: Session, limit: int = 100) -> List[models.Transaction]:
    return (
        db.query(models.Transaction)
        .filter(models.Transaction.status == HELD)
        .order_by(models.Transaction.created_at)
        .limit(limit)
        .all()
    )


def held_amount(db: Session, account_id: str) -> Decimal:
    """계좌에서 빠져 보류 계정에 있는 금액 합 — 원장 잔액 = 가용 잔액 + 이 값."""
    total = (
        db.query(func.sum(models.Transaction.amount))
        .filter(models.Transaction.account_from == account_id, models.Transaction.status == HELD)
        .scalar()
    )
    return ledger.to_money(total or 0)


def expire_holds(db: Session, ttl: timedelta, now: Optional[datetime] = None) -> List[int]:
    """ttl보다 오래 보류된 이체를 취소한다(출금 계좌로 반환). 취소한 거래 id 목록을 돌려준다.

    app/hold_expiry.py가 주기적으로 부른다(docker-compose의 hold-expirer). 담당자가 판단하는 사이에
    만료 처리가 끼어들어도 resolve()의 상태 조건 UPDATE가 둘 중 하나만 통과시킨다.
    """
    # created_at은 DB 서버의 시계로 찍힌다(server_default). 기준 시각도 DB에서 가져와야 앱 서버와 DB 서버의
    # 시계 차이만큼 만료가 당겨지거나 밀리지 않는다. 테스트는 now를 넘겨 시각을 고정한다.
    if now is None:
        db_now = db.query(func.now()).scalar()
        now = db_now if db_now.tzinfo else db_now.replace(tzinfo=timezone.utc)
    cutoff = (now.astimezone(timezone.utc) - ttl).replace(tzinfo=None)
    candidates = [
        tx_id for (tx_id,) in db.query(models.Transaction.id)
        .filter(models.Transaction.status == HELD, models.Transaction.created_at < cutoff)
        .all()
    ]
    db.rollback()   # 조회 트랜잭션을 끝낸다 — 각 해제는 자기 트랜잭션에서

    expired: List[int] = []
    for tx_id in candidates:
        try:
            resolve(db, tx_id, CANCEL, actor_id=None, comment=f"보류 {ttl} 초과", expired=True)
            expired.append(tx_id)
        except NotHeld:
            pass   # 그 사이 담당자가 먼저 풀었다
    return expired
