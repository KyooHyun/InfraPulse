"""FDS(이상금융거래탐지시스템) 룰 엔진.

룰은 DB(fds_rules 테이블)에서 관리되며 운영 중에도 임계값/가중치를 변경할 수 있다.
위험점수(Risk Score)는 0~100 범위로 산정되며, 점수에 따라 조치 수준이 결정된다:
  0~39  LOW    — 기록 및 모니터링
  40~69 MEDIUM — FDS 알림 생성, 담당자 검토 대기
  70~100 HIGH  — FDS 알림 생성 + STR(의심거래보고서) 자동 생성
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from . import models

DEFAULT_RULES = [
    {"name": "고액거래 탐지",       "condition_type": "HIGH_VALUE",    "threshold": 100_000.0, "weight": 30.0},
    {"name": "거래 실패율 이상",     "condition_type": "FAILURE_RATE",  "threshold": 0.3,       "weight": 25.0},
    {"name": "반복 로그인 실패",     "condition_type": "LOGIN_FAILURE", "threshold": 3.0,       "weight": 20.0},
    {"name": "API 응답 지연",       "condition_type": "LATENCY",       "threshold": 1.0,       "weight": 15.0},
    {"name": "단기 고빈도 거래",     "condition_type": "VELOCITY",      "threshold": 5.0,       "weight": 10.0},
]

RISK_LEVEL_MEDIUM = 40.0
RISK_LEVEL_HIGH = 70.0

# FAILURE_RATE: 최근 N건 거래의 실패율 계산 윈도우
FAILURE_RATE_WINDOW = 50
# VELOCITY: 단기 고빈도 탐지 시간 윈도우 (분)
VELOCITY_WINDOW_MINUTES = 10


def seed_default_rules(db: Session) -> None:
    if db.query(models.FdsRule).count() == 0:
        for rule_data in DEFAULT_RULES:
            db.add(models.FdsRule(**rule_data))
        db.commit()


def get_active_rules(db: Session) -> List[models.FdsRule]:
    return db.query(models.FdsRule).filter(models.FdsRule.is_active.is_(True)).all()


def evaluate_transaction(
    amount: float,
    account_from: str,
    db: Session,
    rules: List[models.FdsRule],
) -> Tuple[float, List[str], List[Dict[str, Any]]]:
    """
    거래에 대한 위험점수, 트리거된 룰 유형 목록, 룰별 기여 내역을 반환한다.

    - HIGH_VALUE:   금액이 임계값 이상이면 트리거
    - FAILURE_RATE: 최근 N건 중 실패율이 임계값 이상이면 트리거 (DB 집계)
    - VELOCITY:     동일 계좌의 단기 고빈도 거래 탐지 (DB 집계)
    - LOGIN_FAILURE / LATENCY: auth.py / middleware에서 별도 처리

    Returns:
        (risk_score, triggered_types, contributions)
        contributions: [{"rule_type", "fired", "weight", "threshold", "actual_value"}, ...]
    """
    rule_map = {r.condition_type: r for r in rules}
    score = 0.0
    triggered: List[str] = []
    contributions: List[Dict[str, Any]] = []

    # HIGH_VALUE: 단순 금액 임계값 비교
    if "HIGH_VALUE" in rule_map:
        r = rule_map["HIGH_VALUE"]
        fired = amount >= r.threshold
        if fired:
            score += r.weight
            triggered.append("HIGH_VALUE")
        contributions.append({"rule_type": "HIGH_VALUE", "fired": fired,
                               "weight": r.weight, "threshold": r.threshold, "actual_value": amount})

    # FAILURE_RATE: DB에서 최근 FAILURE_RATE_WINDOW건의 실패율 계산
    if "FAILURE_RATE" in rule_map:
        r = rule_map["FAILURE_RATE"]
        recent_statuses = (
            db.query(models.Transaction.status)
            .order_by(models.Transaction.created_at.desc())
            .limit(FAILURE_RATE_WINDOW)
            .all()
        )
        if len(recent_statuses) >= FAILURE_RATE_WINDOW:
            failure_rate = sum(1 for (s,) in recent_statuses if s == "failed") / len(recent_statuses)
            fired = failure_rate >= r.threshold
            if fired:
                score += r.weight
                triggered.append("FAILURE_RATE")
            contributions.append({"rule_type": "FAILURE_RATE", "fired": fired,
                                   "weight": r.weight, "threshold": r.threshold, "actual_value": round(failure_rate, 4)})
        else:
            contributions.append({"rule_type": "FAILURE_RATE", "fired": False,
                                   "weight": r.weight, "threshold": r.threshold, "actual_value": None})

    # VELOCITY: 동일 계좌에서 VELOCITY_WINDOW_MINUTES 내 거래 건수
    if "VELOCITY" in rule_map:
        r = rule_map["VELOCITY"]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
        recent_count = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.account_from == account_from,
                models.Transaction.created_at >= cutoff,
            )
            .count()
        )
        # 현재 거래 포함 시 임계값 이상이면 트리거
        velocity = recent_count + 1
        fired = velocity >= r.threshold
        if fired:
            score += r.weight
            triggered.append("VELOCITY")
        contributions.append({"rule_type": "VELOCITY", "fired": fired,
                               "weight": r.weight, "threshold": r.threshold, "actual_value": float(velocity)})

    return min(score, 100.0), triggered, contributions


def risk_level(score: float) -> str:
    if score >= RISK_LEVEL_HIGH:
        return "HIGH"
    if score >= RISK_LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"
