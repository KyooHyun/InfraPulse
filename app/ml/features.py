"""거래 피처 추출 — Isolation Forest 입력 벡터 생성."""
from datetime import timedelta

import numpy as np
from sqlalchemy.orm import Session

from .. import models

FEATURE_NAMES = [
    "amount_log",          # log1p(amount)
    "balance_orig_before",
    "balance_orig_after",
    "balance_dest_before",
    "balance_dest_after",
    "error_orig",          # |newOrig + amount - oldOrig| — 잔액 불일치 지표
    "error_dest",          # |newDest - oldDest - amount| — 잔액 불일치 지표
    "hour_of_day",
    "velocity_10min",      # 동일 계좌 10분 내 거래 수
]


def extract_features(tx: models.Transaction, db: Session, velocity: int = -1) -> np.ndarray:
    """
    Transaction 객체에서 피처 벡터를 추출한다.

    velocity=-1(기본값)이면 DB를 조회해 계산한다.
    PaySim 배치 학습처럼 DB 조회가 불필요한 경우 velocity=0을 전달해 쿼리를 생략한다.
    """
    if velocity < 0:
        cutoff = tx.created_at - timedelta(minutes=10)
        velocity = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.account_from == tx.account_from,
                models.Transaction.created_at >= cutoff,
                models.Transaction.id != tx.id,
            )
            .count()
        )

    bef_orig = tx.balance_orig_before or 0.0
    aft_orig = tx.balance_orig_after or 0.0
    bef_dest = tx.balance_dest_before or 0.0
    aft_dest = tx.balance_dest_after or 0.0

    # PaySim의 핵심 사기 신호: 잔액 변동이 거래 금액과 불일치
    error_orig = abs(aft_orig + tx.amount - bef_orig)
    error_dest = abs(aft_dest - bef_dest - tx.amount)

    hour = float(tx.created_at.hour) if tx.created_at else 12.0

    return np.array(
        [
            np.log1p(tx.amount),
            bef_orig,
            aft_orig,
            bef_dest,
            aft_dest,
            error_orig,
            error_dest,
            hour,
            float(velocity),
        ],
        dtype=float,
    )
