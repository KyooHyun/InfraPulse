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
    anomaly_velocity_total,
    fds_alert_total,
    risk_score_histogram,
    transaction_failed_total,
    transaction_total,
)
from ..schemas import TransactionOut, TransferRequest
from ..security import get_current_user
from .. import models, audit

# sklearn/numpy가 설치된 환경에서만 ML 레이어 활성화
# Docker 컨테이너(requirements.txt 설치 후)에서는 항상 활성화됨
_ML_AVAILABLE = False
_if_model = None
try:
    from ..ml.isolation_forest import IFModel
    from ..ml.features import extract_features
    from ..ml.ensemble import ensemble_score as compute_ensemble
    _if_model = IFModel.load()  # 모델 아티팩트 없으면 None
    _ML_AVAILABLE = True
except ImportError:
    pass

router = APIRouter(prefix="/transactions", tags=["거래"])


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

    # 2. FDS 평가 — DB 기반 룰 (전역 상태 없음)
    rules = get_active_rules(db)
    risk_score, triggered, contributions = evaluate_transaction(req.amount, req.account_from, db, rules)

    # 3. 거래 저장
    transaction = create_transaction(db, req, tx_status, reason, risk_score=risk_score)
    transaction_total.inc()
    if not success:
        transaction_failed_total.inc()

    # 3a. ML 앙상블 점수 — sklearn 설치 + 모델 학습 완료 시에만 동작
    feat = None
    ml_score: float | None = None
    ens_score: float | None = None
    if _ML_AVAILABLE and _if_model is not None:
        feat = extract_features(transaction, db)
        ml_score = _if_model.anomaly_score(feat)
        ens_score = compute_ensemble(risk_score, ml_score)
        transaction.ml_anomaly_score = ml_score
        transaction.ensemble_score = ens_score

    # 4. FDS 알림 생성 (트리거된 룰별)
    for alert_type in triggered:
        detail = f"위험점수: {risk_score:.1f} | 트리거: {alert_type}"
        if ml_score is not None:
            detail += f" | ML점수: {ml_score:.3f} | 앙상블: {ens_score:.1f}"
        alert = models.FdsAlert(
            transaction_id=transaction.id,
            alert_type=alert_type,
            risk_score=ens_score if ens_score is not None else risk_score,
            status="DETECTED",
            detail=detail,
        )
        db.add(alert)
        anomaly_event_total.inc()
        fds_alert_total.labels(alert_type=alert_type).inc()
        if alert_type == "HIGH_VALUE":
            anomaly_high_value_total.inc()
        elif alert_type == "FAILURE_RATE":
            anomaly_transaction_failure_total.inc()
        elif alert_type == "VELOCITY":
            anomaly_velocity_total.inc()

    # ML 점수 또는 알림이 생성된 경우 단일 커밋으로 처리
    if triggered or ml_score is not None:
        db.commit()

    risk_score_histogram.observe(risk_score)

    # 5. 고위험(70점 이상) → STR 자동 생성
    if risk_score >= RISK_LEVEL_HIGH:
        create_str(
            db, transaction,
            reason=f"고위험 이상거래 탐지 — 위험점수: {risk_score:.1f}, 룰: {', '.join(triggered)}",
        )

    # 6. CTR: 1천만원 이상 → 자동 고액현금거래 보고
    try_create_ctr(db, transaction)

    # 7. 감사 로그
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
