"""Isolation Forest 기반 비지도 이상거래 탐지 — 라이브 하이브리드 FDS 컴포넌트.

룰 기반 점수(rule_score)와 IF 이상 점수(if_score)를 앙상블한다:
    hybrid_score = ALPHA * rule_score + (1 - ALPHA) * if_score

피처: log_amount, hour_of_day, is_round_amount
  - log_amount     : 거래 금액의 로그 — 규모 정규화
  - hour_of_day    : 거래 시각 (0–23) — 비업무 시간대 이상 탐지
  - is_round_amount: 만원 단위 정수 여부 — 자금세탁 패턴 신호
"""

import logging
import os
from math import log1p
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest
import joblib
from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger(__name__)

MODEL_PATH = Path(os.environ.get("FDS_MODEL_PATH", "/tmp/fds_if_model.joblib"))
ALPHA = 0.6
MIN_TRAIN_SAMPLES = 30

_model: Optional[IsolationForest] = None

# IF decision_function 정규화 기준값 — [−0.5, 0.5] → [100, 0]
_IF_NORM_OFFSET = 0.5
_IF_NORM_SCALE = 1.0


def _features(amount: float, hour: int, is_round: bool) -> np.ndarray:
    return np.array([[log1p(amount), float(hour), float(is_round)]])


def train_from_db(db: Session) -> None:
    """DB에 쌓인 최근 거래 데이터로 IF 모델을 학습하고 디스크에 저장한다."""
    global _model
    rows = (
        db.query(models.Transaction.amount, models.Transaction.created_at)
        .order_by(models.Transaction.created_at.desc())
        .limit(5_000)
        .all()
    )
    if len(rows) < MIN_TRAIN_SAMPLES:
        logger.info(
            "IF 학습 데이터 부족 (%d/%d) — 거래가 쌓이면 자동 재학습됩니다",
            len(rows), MIN_TRAIN_SAMPLES,
        )
        return

    X = np.array([
        [
            log1p(amt),
            float(created_at.hour) if created_at else 12.0,
            float(int(amt % 10_000 == 0)),
        ]
        for amt, created_at in rows
    ])
    clf = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    clf.fit(X)
    _model = clf
    joblib.dump(clf, MODEL_PATH)
    logger.info("IF 모델 학습 완료 — %d건 → %s", len(rows), MODEL_PATH)


def load_or_train(db: Session) -> None:
    """저장된 모델을 로드; 없으면 DB에서 학습을 시도한다."""
    global _model
    if MODEL_PATH.exists():
        _model = joblib.load(MODEL_PATH)
        logger.info("IF 모델 로드 완료 ← %s", MODEL_PATH)
    else:
        train_from_db(db)


def compute_if_score(amount: float, hour: int, is_round: bool) -> float:
    """IF 이상 점수를 [0, 100] 범위로 반환한다. 높을수록 이상거래 가능성 높음.
    모델이 준비되지 않은 경우 0.0을 반환한다.
    """
    if _model is None:
        return 0.0
    raw = _model.decision_function(_features(amount, hour, is_round))[0]
    score = (_IF_NORM_OFFSET - raw) / _IF_NORM_SCALE * 100.0
    return round(max(0.0, min(100.0, score)), 2)


def hybrid_score(rule_score: float, if_score: float) -> float:
    """ALPHA * rule_score + (1 - ALPHA) * if_score, 범위 [0, 100]."""
    return round(min(100.0, ALPHA * rule_score + (1.0 - ALPHA) * if_score), 2)


def is_ready() -> bool:
    return _model is not None
