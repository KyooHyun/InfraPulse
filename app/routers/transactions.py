import logging
from random import random
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..crud import create_transaction, get_transactions
from ..db import get_db
from .. import ledger
from ..fds_engine import get_active_rules, evaluate_transaction, RISK_LEVEL_HIGH
from ..report_generator import create_str
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
from ..schemas import AccountOut, TransactionOut, TransferRequest
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transactions", tags=["거래"])


def _unchanged_balances(sender: models.Account, receiver: models.Account) -> Dict[str, object]:
    """이체가 성립하지 않은 경우의 잔액 기록 — 전후가 같다."""
    return {
        "balance_orig_before": sender.balance,
        "balance_orig_after": sender.balance,
        "balance_dest_before": receiver.balance,
        "balance_dest_after": receiver.balance,
    }


@router.get("", response_model=List[TransactionOut], summary="거래 목록 조회")
def list_transactions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return get_transactions(db)


@router.get("/accounts/{account_id}", response_model=AccountOut, summary="계좌 잔액 조회")
def get_account(
    account_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    account = (
        db.query(models.Account)
        .filter(models.Account.account_id == account_id)
        .one_or_none()
    )
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계좌를 찾을 수 없습니다")
    return account


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
    amount = ledger.to_money(req.amount)

    # 1. 계좌 확보 — 잠금 구간 밖에서 미리 개설한다 (ledger.ensure_accounts 주석 참고)
    ledger.ensure_accounts(db, (req.account_from, req.account_to), req.currency)

    # 2. FDS 평가 — 거래 이력 집계라 계좌 잔액과 무관하므로 잠금 전에 끝낸다.
    #    잠금을 쥔 채로 하면 집계 쿼리 시간만큼 같은 계좌의 다른 이체가 대기한다.
    rules = get_active_rules(db)
    risk_score, triggered, contributions = evaluate_transaction(req.amount, req.account_from, db, rules)

    # 3. 잠금 구간 — 잔액 확인·이동·거래기록이 한 트랜잭션 안에서 끝난다.
    #    여기서 SELECT ... FOR UPDATE가 없으면 같은 계좌에 동시 이체가 들어올 때
    #    둘 다 같은 잔액을 읽고 둘 다 통과시켜 초과 인출이 발생한다.
    try:
        accounts = ledger.lock_accounts(db, (req.account_from, req.account_to))
        sender = accounts[req.account_from]
        receiver = accounts[req.account_to]

        # 무작위 실패 시뮬레이션 — 잔액과 무관한 대외계 오류를 흉내낸다.
        failure_chance = 0.25 if req.amount > 50_000 else 0.15
        if random() < failure_chance:
            tx_status, reason = "failed", "random failure"
        else:
            tx_status, reason = "success", "completed"

        if tx_status == "success":
            try:
                balances = ledger.apply_transfer(sender, receiver, amount)
            except ledger.InsufficientBalance as exc:
                tx_status, reason = "failed", "insufficient balance"
                balances = _unchanged_balances(sender, receiver)
                logger.info("이체 거절 — %s", exc)
        else:
            balances = _unchanged_balances(sender, receiver)

        transaction = create_transaction(
            db, req, tx_status, reason,
            risk_score=risk_score, balances=balances, commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(transaction)

    transaction_total.inc()
    if tx_status != "success":
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

    # 알림과 STR은 같은 점수를 근거로 삼아야 한다. 예전에는 알림에 앙상블 점수를
    # 적으면서 STR 판정은 룰 점수로 해서, 알림에 적힌 점수가 70점을 넘어도 STR이
    # 생성되지 않았다. (calibration/reachability.py 가 이 불일치를 수치로 보여준다)
    effective_score = ens_score if ens_score is not None else risk_score

    # 4. FDS 알림 생성 (트리거된 룰별)
    for alert_type in triggered:
        detail = f"위험점수: {risk_score:.1f} | 트리거: {alert_type}"
        if ml_score is not None:
            detail += f" | ML점수: {ml_score:.3f} | 앙상블: {ens_score:.1f}"
        alert = models.FdsAlert(
            transaction_id=transaction.id,
            alert_type=alert_type,
            risk_score=effective_score,
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
    if effective_score >= RISK_LEVEL_HIGH:
        create_str(
            db, transaction,
            reason=f"고위험 이상거래 탐지 — 위험점수: {effective_score:.1f}, 룰: {', '.join(triggered)}",
        )

    # CTR은 현금 거래 대상이라 이체 경로에서는 만들지 않는다 (report_generator.py 참고)

    # 6. 감사 로그
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
