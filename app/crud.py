from sqlalchemy.orm import Session
from . import models, schemas


def create_transaction(
    db: Session,
    tx_in: schemas.TransferRequest,
    status: str,
    reason: str,
    risk_score: float = 0.0,
) -> models.Transaction:
    transaction = models.Transaction(
        account_from=tx_in.account_from,
        account_to=tx_in.account_to,
        amount=tx_in.amount,
        currency=tx_in.currency,
        status=status,
        reason=reason,
        risk_score=risk_score,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


def get_transactions(db: Session, limit: int = 100):
    return (
        db.query(models.Transaction)
        .order_by(models.Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
