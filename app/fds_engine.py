"""FDS(이상금융거래탐지시스템) 룰 엔진.

룰은 DB(fds_rules 테이블)에서 관리되며 운영 중에도 임계값/가중치를 변경할 수 있다.
위험점수(Risk Score)는 0~100 범위로 산정되며, 점수에 따라 조치 수준이 결정된다:
  0~39  LOW    — 기록 및 모니터링
  40~69 MEDIUM — FDS 알림 생성, 담당자 검토 대기
  70~100 HIGH  — FDS 알림 생성 + STR(의심거래보고서) 초안 생성 → 담당자 검토
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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


# 거래 한 건을 보고 판정할 수 있는 룰. LOGIN_FAILURE와 LATENCY는 각각 auth.py와
# 미들웨어가 다루는 시스템 수준 신호라 거래 위험점수에 기여하지 않는다.
# calibration/reachability.py 가 이 목록을 근거로 "도달 가능한 점수"를 계산한다.
TRANSACTION_SCOPED_RULES = ("HIGH_VALUE", "FAILURE_RATE", "VELOCITY")


def evaluate_signals(
    signals: Dict[str, Any],
    rules: List[models.FdsRule],
) -> Tuple[float, List[str], List[Dict[str, Any]]]:
    """관측값 → (위험점수, 트리거된 룰, 룰별 기여 내역).

    DB를 모른다. 신호를 어디서 모았는지와 신호를 어떻게 점수로 바꾸는지를 갈라
    놓기 위해서다 — 라이브 경로(evaluate_transaction)와 오프라인 캘리브레이션
    (calibration/)이 **같은 이 함수**를 쓴다. 분석용으로 룰을 다시 구현하면
    "분석에서 근거를 확인한 룰"과 "운영에서 실제로 도는 룰"이 조용히 갈라진다.

    signals: {"HIGH_VALUE": 금액, "FAILURE_RATE": 실패율, "VELOCITY": 건수}
             값이 None이거나 키가 없으면 그 룰은 판정하지 않는다(fired=False).
    """
    rule_map = {r.condition_type: r for r in rules}
    score = 0.0
    triggered: List[str] = []
    contributions: List[Dict[str, Any]] = []

    for rule_type in TRANSACTION_SCOPED_RULES:
        rule = rule_map.get(rule_type)
        if rule is None:
            continue

        observed = signals.get(rule_type)
        fired = observed is not None and observed >= rule.threshold
        if fired:
            score += rule.weight
            triggered.append(rule_type)

        contributions.append({
            "rule_type": rule_type,
            "fired": fired,
            "weight": rule.weight,
            "threshold": rule.threshold,
            "actual_value": observed,
        })

    return min(score, 100.0), triggered, contributions


def collect_signals(
    amount: float,
    account_from: str,
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """DB에서 룰 판정에 필요한 관측값을 모은다. 현재 거래는 아직 기록되기 전이다.

    - HIGH_VALUE:   거래 금액 그대로
    - FAILURE_RATE: 최근 FAILURE_RATE_WINDOW건의 실패율. 표본이 모자라면 None —
                    거래 10건으로 계산한 실패율은 판정 근거가 되지 못한다.
    - VELOCITY:     동일 계좌의 VELOCITY_WINDOW_MINUTES 내 거래 건수(현재 건 포함)

    now: VELOCITY 윈도우의 기준 시각. 운영에서는 비워 둔다(현재 시각). 과거 거래를
         같은 정의로 다시 계산할 때(동등성 테스트) 거래 시각을 넣는다.
    오프라인 평가는 이 함수를 277만 번 부를 수 없어 calibration/dataset.py가 같은
    정의를 따로 계산한다. 두 구현이 같은 값을 내는지는
    tests/test_scoring_parity.py가 고정한다.
    """
    # 같은 시각의 거래가 윈도우 경계에 걸리면 어느 쪽이 "최근"인지 정해지지 않으므로
    # 기록 순서(id)로 동률을 깬다. calibration/dataset.py도 (created_at, id) 순으로 센다.
    recent_statuses = (
        db.query(models.Transaction.status)
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .limit(FAILURE_RATE_WINDOW)
        .all()
    )
    if len(recent_statuses) >= FAILURE_RATE_WINDOW:
        failure_rate = round(
            sum(1 for (s,) in recent_statuses if s == "failed") / len(recent_statuses), 4
        )
    else:
        failure_rate = None

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
    recent_count = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.account_from == account_from,
            models.Transaction.created_at >= cutoff,
        )
        .count()
    )

    return {
        "HIGH_VALUE": amount,
        "FAILURE_RATE": failure_rate,
        "VELOCITY": float(recent_count + 1),   # 현재 거래를 포함해 센다
    }


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
    return evaluate_signals(collect_signals(amount, account_from, db), rules)


def risk_level(score: float) -> str:
    if score >= RISK_LEVEL_HIGH:
        return "HIGH"
    if score >= RISK_LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"
