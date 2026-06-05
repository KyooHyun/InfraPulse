from collections import deque
from random import random
from typing import List

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from ..crud import create_transaction, get_transactions
from ..db import get_db
from ..fds_engine import get_active_rules, evaluate_transaction, RISK_LEVEL_HIGH
from ..report_generator import try_create_ctr, create_str
from ..metrics import (
    anomaly_event_total,
    anomaly_high_value_total,
    anomaly_transaction_failure_total,
    fds_alert_total,
    risk_score_histogram,
    transaction_failed_total,
    transaction_total,
)
from ..schemas import TransactionOut, TransferRequest
from ..security import get_current_user
from .. import models, audit

router = APIRouter(prefix="/transactions", tags=["거래"])

# 최근 50건의 성공/실패 결과 (실패율 계산용)
_recent_results: deque = deque(maxlen=50)


@router.get("", response_model=List[TransactionOut], summary="거래 목록 조회")
def list_transactions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return get_transactions(db)


@router.post(
    "/transfer",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
    summary="계좌 이체",
)
def transfer(
    req: TransferRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # 1. 무작위 실패 시뮬레이션
    failure_chance = 0.25 if req.amount > 50_000 else 0.15
    success = random() >= failure_chance
    tx_status = "success" if success else "failed"
    reason = "completed" if success else "random failure"

    # 2. 실패율 추적 갱신
    _recent_results.append(success)

    # 3. FDS 평가 (DB 기반 룰)
    rules = get_active_rules(db)
    risk_score, triggered = evaluate_transaction(req.amount, _recent_results, rules)

    # 4. 거래 저장 (risk_score 포함)
    transaction = create_transaction(db, req, tx_status, reason, risk_score=risk_score)
    transaction_total.inc()
    if not success:
        transaction_failed_total.inc()

    # 5. FDS 알림 생성 (트리거된 룰별)
    for alert_type in triggered:
        alert = models.FdsAlert(
            transaction_id=transaction.id,
            alert_type=alert_type,
            risk_score=risk_score,
            status="DETECTED",
            detail=f"위험점수: {risk_score:.1f} | 트리거: {alert_type}",
        )
        db.add(alert)
        anomaly_event_total.inc()
        fds_alert_total.labels(alert_type=alert_type).inc()
        if alert_type == "HIGH_VALUE":
            anomaly_high_value_total.inc()
        elif alert_type == "FAILURE_RATE":
            anomaly_transaction_failure_total.inc()

    if triggered:
        db.commit()

    risk_score_histogram.observe(risk_score)

    # 6. 고위험(70점 이상) → STR 자동 생성
    if risk_score >= RISK_LEVEL_HIGH:
        create_str(
            db, transaction,
            reason=f"고위험 이상거래 탐지 — 위험점수: {risk_score:.1f}, 룰: {', '.join(triggered)}",
        )

    # 7. CTR: 1천만원 이상 → 자동 고액현금거래 보고
    try_create_ctr(db, transaction)

    # 8. 감사 로그
    audit.log_event(
        db,
        action="CREATE_TRANSACTION",
        entity_type="Transaction",
        entity_id=str(transaction.id),
        detail=f"amount={req.amount:,.0f}, status={tx_status}, risk_score={risk_score:.1f}",
        ip_address=request.client.host if request.client else None,
        user_id=current_user.id,
    )

    return transaction
