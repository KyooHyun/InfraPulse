from sqlalchemy.orm import Session
from . import models, schemas


def create_transaction(
    db: Session,
    tx_in: schemas.TransferRequest,
    status: str,
    reason: str,
    risk_score: float = 0.0,
    balances: dict | None = None,
    commit: bool = True,
) -> models.Transaction:
    """거래 행을 만든다.

    commit=False는 이체 경로에서 쓴다. 원장 잔액 변경과 거래 기록은 같은 트랜잭션
    안에서 함께 커밋돼야 한다 — 따로 커밋하면 "잔액은 줄었는데 거래 기록이 없는"
    상태가 중간에 존재할 수 있고, 뒤쪽이 실패하면 그 상태로 남는다.
    """
    transaction = models.Transaction(
        account_from=tx_in.account_from,
        account_to=tx_in.account_to,
        amount=tx_in.amount,
        currency=tx_in.currency,
        status=status,
        reason=reason,
        risk_score=risk_score,
    )
    if balances:
        for column, value in balances.items():
            setattr(transaction, column, float(value))

    db.add(transaction)
    if commit:
        db.commit()
        db.refresh(transaction)
    else:
        # id와 created_at(server_default)을 뒤 단계에서 써야 하므로 flush까지는 한다.
        db.flush()
    return transaction


def get_transactions(db: Session, limit: int = 100):
    return (
        db.query(models.Transaction)
        .order_by(models.Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
