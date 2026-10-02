import logging
from random import random
from time import sleep
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..crud import create_transaction, get_transactions
from ..db import get_db
from .. import idempotency, ledger
from ..fds_engine import get_active_rules, evaluate_transaction, RISK_LEVEL_HIGH, RISK_LEVEL_MEDIUM
from ..report_generator import create_str
from ..metrics import (
    anomaly_event_total,
    anomaly_high_value_total,
    anomaly_velocity_total,
    fds_alert_total,
    transfer_lock_retry_total,
    risk_score_histogram,
    transaction_failed_total,
    transaction_total,
)
from ..schemas import AccountOut, TransactionOut, TransferRequest
from ..config import settings
from ..security import get_current_user
from .. import models, audit

logger = logging.getLogger(__name__)

# ML 레이어는 FDS_ML_ENABLED=true일 때만 켠다 (app/config.py 참고).
# 켜져 있는데 모델 파일이나 sklearn이 없으면 조용히 넘어가지 않고 경고를 남긴다.
_ML_AVAILABLE = False
_if_model = None
if settings.fds_ml_enabled:
    try:
        from ..ml.isolation_forest import IFModel
        from ..ml.features import extract_features
        from ..ml.ensemble import ensemble_score as compute_ensemble
        _if_model = IFModel.load()  # 모델 아티팩트 없으면 None
        _ML_AVAILABLE = True
        if _if_model is None:
            logger.warning("FDS_ML_ENABLED=true지만 모델 파일이 없다 — 룰 점수만 사용한다")
    except ImportError:
        logger.warning("FDS_ML_ENABLED=true지만 ML 의존성이 없다 — 룰 점수만 사용한다")

router = APIRouter(prefix="/transactions", tags=["거래"])


# MySQL 에러 코드 — 재시도하면 성공할 수 있는 잠금 충돌
_LOCK_CONFLICT_ERRNO = {1213: "deadlock", 1205: "lock_wait_timeout"}


def _lock_conflict(exc: OperationalError) -> Optional[str]:
    """데드락·잠금 대기 초과면 그 이름, 아니면 None (그대로 올릴 오류)."""
    args = getattr(exc.orig, "args", ())
    return _LOCK_CONFLICT_ERRNO.get(args[0]) if args and isinstance(args[0], int) else None


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
    response: Response,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=idempotency.MAX_KEY_LENGTH,
        description="이체마다 고유한 키. 같은 키로 재전송하면 이체를 다시 실행하지 않고 처음 결과를 돌려준다.",
    ),
):
    amount = ledger.to_money(req.amount)
    req_hash = idempotency.request_hash(req) if idempotency_key else None

    def replay_if_seen() -> Optional[models.Transaction]:
        """같은 키로 이미 확정된 이체가 있으면 그 거래를 돌려준다(Idempotent-Replayed: true)."""
        if not idempotency_key:
            return None
        try:
            stored = idempotency.stored_result(db, current_user.id, idempotency_key, req_hash)
        except idempotency.KeyReused:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="같은 Idempotency-Key로 내용이 다른 이체 요청이 왔습니다 — 새 이체에는 새 키를 쓰세요",
            )
        if stored is not None:
            response.headers["Idempotent-Replayed"] = "true"
        return stored

    # 0. 이미 처리된 키면 FDS 채점·잠금 없이 바로 돌려준다 (최적화 — 보장은 유니크 제약)
    replayed = replay_if_seen()
    if replayed is not None:
        return replayed

    # 1. 계좌 확보 — 잠금 구간 밖에서 미리 개설한다 (ledger.ensure_accounts 주석 참고)
    ledger.ensure_accounts(db, (req.account_from, req.account_to), req.currency)

    # 2. FDS 평가 — 잠금 전에 끝낸다. 잠금을 쥔 채로 하면 집계 쿼리 시간만큼 같은 계좌의
    #    다른 이체가 대기한다. 잔액 룰(BALANCE_DRAIN, DEST_EMPTY)에 쓰는 거래 전 잔액은 잠그지
    #    않고 읽는다 — 동시 이체가 있으면 잠금 후 실제 잔액과 조금 다를 수 있지만, 판정 근거로는
    #    충분하고 잔액 이동 자체의 정합성은 아래 잠금 구간이 지킨다.
    pre_balances = {
        a.account_id: float(a.balance)
        for a in db.query(models.Account)
        .filter(models.Account.account_id.in_((req.account_from, req.account_to)))
        .all()
    }
    rules = get_active_rules(db)
    risk_score, triggered, contributions = evaluate_transaction(
        float(amount), req.account_from, db, rules,
        account_to=req.account_to,
        balance_orig_before=pre_balances.get(req.account_from),
        balance_dest_before=pre_balances.get(req.account_to),
    )

    # 3. 잠금 구간 — 잔액 확인·이동·거래기록이 한 트랜잭션 안에서 끝난다.
    #    여기서 SELECT ... FOR UPDATE가 없으면 같은 계좌에 동시 이체가 들어올 때
    #    둘 다 같은 잔액을 읽고 둘 다 통과시켜 초과 인출이 발생한다.
    def run_locked():
        """잠금 구간 한 번. (재현된 거래, None) 또는 (새 거래, 상태)를 돌려준다. 커밋까지 한다."""
        accounts = ledger.lock_accounts(db, (req.account_from, req.account_to))
        sender = accounts[req.account_from]
        receiver = accounts[req.account_to]

        # 잠금을 기다리는 동안 같은 키의 요청이 먼저 커밋했을 수 있다. 같은 계좌를 잠그므로
        # SQLite(직렬화)에서는 여기서 보인다. MySQL REPEATABLE READ에서는 스냅샷 때문에 안 보일 수
        # 있고, 그때는 아래 커밋의 유니크 제약 위반이 잡는다.
        replayed = replay_if_seen()
        if replayed is not None:
            db.rollback()   # 잠금 해제
            return replayed, None

        # 무작위 실패 시뮬레이션 — 잔액과 무관한 대외계 오류를 흉내낸다.
        failure_chance = 0.25 if amount > 50_000 else 0.15
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
        if idempotency_key:
            # 잔액 변경·거래 기록·키가 한 커밋으로 묶인다 — 셋 중 일부만 남는 상태가 없다.
            idempotency.record(db, current_user.id, idempotency_key, req_hash, transaction)
        db.commit()
        return transaction, tx_status

    # 데드락(1213)·잠금 대기 초과(1205)는 MySQL이 트랜잭션 하나를 희생시켜 롤백한 것이다. 그 트랜잭션의
    # 변경은 남지 않으므로 처음부터 다시 실행해도 이중 출금이 되지 않는다. 계좌 잠금 순서를 고정해
    # 데드락은 원래 생기지 않아야 하지만(ledger.lock_accounts), 다른 경로가 같은 행을 다른 순서로
    # 잠그는 경우까지 막을 수는 없어 방어적으로 재시도한다.
    attempts = settings.transfer_lock_retries + 1
    for attempt in range(attempts):
        try:
            transaction, tx_status = run_locked()
            break
        except IntegrityError:
            # 같은 키의 동시 요청이 먼저 커밋했다 — 이 요청의 잔액 변경은 통째로 롤백되고,
            # 먼저 확정된 결과를 돌려준다. 다른 무결성 오류면 그대로 올린다.
            db.rollback()
            replayed = replay_if_seen()
            if replayed is None:
                raise
            return replayed
        except OperationalError as exc:
            db.rollback()
            reason = _lock_conflict(exc)
            if reason is None:
                raise
            transfer_lock_retry_total.labels(reason=reason).inc()
            if attempt == attempts - 1:
                logger.warning("이체 잠금 충돌 — 재시도 %d회 후 포기 (%s)", settings.transfer_lock_retries, reason)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="계좌가 다른 이체로 잠겨 있습니다 — 같은 Idempotency-Key로 다시 시도하세요",
                )
            logger.info("이체 잠금 충돌 — 재시도 %d/%d (%s)", attempt + 1, settings.transfer_lock_retries, reason)
            sleep(0.05 * (2 ** attempt) * (1 + random()))   # 지수 백오프 + 지터
        except Exception:
            db.rollback()
            raise

    if transaction is not None and tx_status is None:   # 잠금 후 재조회로 재현된 결과
        return transaction

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

    # 4. FDS 알림 생성 — MEDIUM 이상일 때만, 트리거된 룰별로.
    #    LOW는 기록만 한다(모듈 docstring의 등급 정의). 예전에는 점수와 무관하게 발화한 룰마다
    #    알림을 만들어서, 거래의 1/3에서 발화하는 NEW_RECIPIENT 같은 약한 신호가 검토 대기열을 채웠다.
    alerted = triggered if effective_score >= RISK_LEVEL_MEDIUM else []
    for alert_type in alerted:
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
        if alert_type in ("HIGH_VALUE", "HIGH_VALUE_TOP"):
            anomaly_high_value_total.inc()
        elif alert_type == "VELOCITY":
            anomaly_velocity_total.inc()

    # ML 점수 또는 알림이 생성된 경우 단일 커밋으로 처리
    if alerted or ml_score is not None:
        db.commit()

    risk_score_histogram.observe(risk_score)

    # 5. 고위험(70점 이상) → STR 초안 생성 (제출 여부는 담당자 검토로 결정)
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
        detail=f"amount={amount}, status={tx_status}, risk_score={risk_score:.1f}",   # 원장과 같은 십진 표현
        ip_address=request.client.host if request.client else None,
        user_id=current_user.id,
    )

    return transaction
