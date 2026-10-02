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

# 판정 시점(이체 실행 전)에 알 수 있는 값만 쓴다. 예전 피처 9개 중 4개(balance_orig_after,
# balance_dest_after, error_orig, error_dest)는 이체가 끝난 뒤의 잔액이었다 — 판정 시점에 없는 정보다.
#
# 이 5개는 학습 구간 안의 검증 구간(PaySim 10~12일)에서 고른 세트다. 평가 구간은 보지 않았다.
#   원래 9개            검증 PR-AUC 0.0269
#   5개 (아래)          검증 PR-AUC 0.1956
#   5개 + 잔액 비율·0 여부 3개  검증 PR-AUC 0.0396
# 잔액 비율(amount/잔액)을 넣으면 오히려 나빠진다. PaySim 정상 거래의 47%는 거래 전 잔액이 0인데도
# 송금해서 비율이 극단값(중앙값 605)이 되고, 사기는 정확히 1.0이다. 비지도인 IF는 극단값을 이상으로
# 보므로 정상 거래를 고립시킨다. 이 신호는 지도 방식(룰)으로 다룰 일이다. (evaluation/README.md)
FEATURE_NAMES = [
    "amount_log",              # log1p(amount)
    "balance_orig_before",
    "balance_dest_before",
    "hour_of_day",
    "velocity_10min",          # 동일 계좌의 직전 거래 수 (현재 건 제외)
]


def feature_values(
    amount: float,
    balance_orig_before: Optional[float],
    balance_dest_before: Optional[float],
    hour: Optional[int],
    velocity: float,
) -> List[float]:
    """관측값 → 피처 벡터(FEATURE_NAMES 순서). DB를 모른다.

    입력은 전부 이체 실행 **전**에 알 수 있는 값이다. 그래서 이 피처로 만든 점수는 커밋 전에
    매길 수도 있다(지금은 사후 모니터링으로 둔다 — README "ML / PaySim 검증 전략").

    velocity는 동일 계좌의 윈도우 내 **직전** 거래 수다(현재 건 제외). 룰의 VELOCITY
    신호는 현재 건을 포함해 세므로, 같은 윈도우라면 이 값은 VELOCITY - 1이다.
    """
    return [
        float(np.log1p(amount)),
        balance_orig_before or 0.0,
        balance_dest_before or 0.0,
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
            tx.balance_dest_before,
            tx.created_at.hour if tx.created_at else None,
            velocity,
        ),
        dtype=float,
    )
