"""거래 피처 추출 — Isolation Forest 입력 벡터 생성.

피처를 **어떻게 계산하는가**(feature_values, 순수 함수)와 **값을 어디서 모으는가**
(extract_features는 DB, calibration/dataset.py는 CSV·행 목록)를 갈라 둔다.
운영·학습·평가가 모두 feature_values를 거치므로 피처 정의는 한 곳에만 있다.

예전에는 학습·평가 스크립트가 extract_features(tx, db, velocity=0)으로 velocity를
0으로 고정했고, 운영은 실제 velocity를 계산했다. 모델이 학습 때 본 적 없는 값을
운영에서 받는 학습-서빙 불일치였다. 이제 velocity도 양쪽이 같은 정의로 계산한다.
"""
from datetime import timedelta
from typing import List, Optional

import numpy as np
from sqlalchemy.orm import Session

from .. import models
from ..fds_engine import VELOCITY_WINDOW_MINUTES

FEATURE_NAMES = [
    "amount_log",          # log1p(amount)
    "balance_orig_before",
    "balance_orig_after",
    "balance_dest_before",
    "balance_dest_after",
    "error_orig",          # |newOrig + amount - oldOrig| — 잔액 불일치 지표
    "error_dest",          # |newDest - oldDest - amount| — 잔액 불일치 지표
    "hour_of_day",
    "velocity_10min",      # 동일 계좌의 직전 거래 수 (현재 건 제외)
]


def feature_values(
    amount: float,
    balance_orig_before: Optional[float],
    balance_orig_after: Optional[float],
    balance_dest_before: Optional[float],
    balance_dest_after: Optional[float],
    hour: Optional[int],
    velocity: float,
) -> List[float]:
    """관측값 → 피처 벡터(FEATURE_NAMES 순서). DB를 모른다.

    velocity는 동일 계좌의 윈도우 내 **직전** 거래 수다(현재 건 제외). 룰의 VELOCITY
    신호는 현재 건을 포함해 세므로, 같은 윈도우라면 이 값은 VELOCITY - 1이다.
    """
    bef_orig = balance_orig_before or 0.0
    aft_orig = balance_orig_after or 0.0
    bef_dest = balance_dest_before or 0.0
    aft_dest = balance_dest_after or 0.0

    # PaySim의 핵심 사기 신호: 잔액 변동이 거래 금액과 불일치
    error_orig = abs(aft_orig + amount - bef_orig)
    error_dest = abs(aft_dest - bef_dest - amount)

    return [
        float(np.log1p(amount)),
        bef_orig,
        aft_orig,
        bef_dest,
        aft_dest,
        error_orig,
        error_dest,
        float(hour) if hour is not None else 12.0,
        float(velocity),
    ]


def extract_features(tx: models.Transaction, db: Session) -> np.ndarray:
    """기록된 Transaction에서 피처 벡터를 만든다 (운영 경로).

    velocity는 DB에서 센다 — 같은 계좌에서 이 거래보다 **먼저 기록된**(id가 작은) 거래 중
    [거래 시각 - 윈도우, 거래 시각]에 든 수. 윈도우는 룰의 VELOCITY와 같은
    VELOCITY_WINDOW_MINUTES다. 상한이 없으면 과거 거래를 다시 계산할 때 그 뒤의
    거래까지 세게 된다(look-ahead).
    """
    cutoff = tx.created_at - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
    velocity = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.account_from == tx.account_from,
            models.Transaction.created_at >= cutoff,
            models.Transaction.created_at <= tx.created_at,
            models.Transaction.id < tx.id,
        )
        .count()
    )

    return np.array(
        feature_values(
            tx.amount,
            tx.balance_orig_before,
            tx.balance_orig_after,
            tx.balance_dest_before,
            tx.balance_dest_after,
            tx.created_at.hour if tx.created_at else None,
            velocity,
        ),
        dtype=float,
    )
