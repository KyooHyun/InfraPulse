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

# 거래 룰의 임계값·가중치는 calibration/weights.py가 PaySim 1~9일(step <= 216)로 정한 값이다.
#   임계값: HIGH_VALUE·HIGH_VALUE_TOP은 그 구간 금액의 95·99 분위수, 나머지는 정의에 따른 값
#   가중치: 10·ln(lift)를 5점 단위로 반올림 — lift가 곱해지면 점수는 더해지는 로그 오즈 척도다.
#           그래서 40점(MEDIUM)은 lift 약 55배, 70점(HIGH)은 약 1,100배에 해당한다.
#           룰 사이의 상관은 무시한다(나이브 베이즈 가정). 10~12일 검증 구간에서 결과를 확인했다.
#     HIGH_VALUE      lift   7.3  → 20
#     HIGH_VALUE_TOP  lift  21.3  → 30 (HIGH_VALUE에 더하는 단계라 +10)
#     BALANCE_DRAIN   lift 103.5  → 45
#     DEST_EMPTY      lift   4.8  → 15
#     NEW_RECIPIENT   lift   2.4  → 10
#     VELOCITY        PaySim에서 측정 불가(송금 계좌 최대 3회 등장) → 기존 판단치 10 유지
# PaySim의 금액 단위는 원화가 아니다. 원화 운영 데이터로 다시 정해야 하는 값이다.
#
# FAILURE_RATE·LOGIN_FAILURE·LATENCY는 시스템 모니터링 신호다(전체 거래 실패율, 로그인, 응답
# 지연). 특정 거래가 사기인지와 관계가 없으므로 가중치 0 — 거래 위험점수에 기여하지 않는다.
DEFAULT_RULES = [
    {"name": "고액거래 (상위 5%)",                "condition_type": "HIGH_VALUE",     "threshold": 827_513.0,   "weight": 20.0},
    {"name": "초고액거래 (상위 1%, 추가)",         "condition_type": "HIGH_VALUE_TOP", "threshold": 1_775_460.0, "weight": 10.0},
    {"name": "잔액 비우기 (90~100% 인출)",         "condition_type": "BALANCE_DRAIN",  "threshold": 0.9,         "weight": 45.0},
    {"name": "빈 수취 계좌로 송금",                "condition_type": "DEST_EMPTY",     "threshold": 1.0,         "weight": 15.0},
    {"name": "신규·휴면 수취인 (24시간 입금 없음)", "condition_type": "NEW_RECIPIENT",  "threshold": 1.0,         "weight": 10.0},
    {"name": "단기 고빈도 거래",                   "condition_type": "VELOCITY",       "threshold": 5.0,         "weight": 10.0},
    {"name": "거래 실패율 이상 (시스템)",           "condition_type": "FAILURE_RATE",   "threshold": 0.3,         "weight": 0.0},
    {"name": "반복 로그인 실패 (시스템)",           "condition_type": "LOGIN_FAILURE",  "threshold": 3.0,         "weight": 0.0},
    {"name": "API 응답 지연 (시스템)",             "condition_type": "LATENCY",        "threshold": 1.0,         "weight": 0.0},
]

RISK_LEVEL_MEDIUM = 40.0
RISK_LEVEL_HIGH = 70.0

# FAILURE_RATE: 최근 N건 거래의 실패율 계산 윈도우
FAILURE_RATE_WINDOW = 50
# VELOCITY: 단기 고빈도 탐지 시간 윈도우 (분)
VELOCITY_WINDOW_MINUTES = 10
# NEW_RECIPIENT: 수취 계좌의 직전 입금을 세는 윈도우 (분)
RECIPIENT_WINDOW_MINUTES = 24 * 60


def seed_default_rules(db: Session) -> None:
    if db.query(models.FdsRule).count() == 0:
        for rule_data in DEFAULT_RULES:
            db.add(models.FdsRule(**rule_data))
        db.commit()


def get_active_rules(db: Session) -> List[models.FdsRule]:
    return db.query(models.FdsRule).filter(models.FdsRule.is_active.is_(True)).all()


# 거래 위험점수에 기여하는 룰. FAILURE_RATE·LOGIN_FAILURE·LATENCY는 시스템 수준 신호라 빠진다.
# calibration/reachability.py 가 이 목록을 근거로 "도달 가능한 점수"를 계산한다.
TRANSACTION_SCOPED_RULES = (
    "HIGH_VALUE", "HIGH_VALUE_TOP", "BALANCE_DRAIN", "DEST_EMPTY", "NEW_RECIPIENT", "VELOCITY",
)


def derive_signals(
    amount: float,
    balance_orig_before: Optional[float],
    balance_dest_before: Optional[float],
    recipient_prior_inflow: Optional[int],
    velocity: Optional[float],
    failure_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """원천값 → 룰 신호. DB를 모른다.

    운영(collect_signals)과 오프라인 평가(calibration/dataset.py)가 원천값을 각자 모은 뒤
    **이 함수**로 신호를 만든다. 신호 정의가 한 곳에만 있다.
    입력은 전부 이체 실행 전에 알 수 있는 값이다. 원천값이 없으면 그 신호는 None — 룰을 판정하지 않는다.

    - BALANCE_DRAIN: 거래 전 잔액 대비 인출 비율. 잔액이 0 이하이거나 금액이 잔액을 넘으면 0.
                     실제 은행에서 잔액 초과 인출은 거절된다. PaySim 정상 거래의 47%는 잔액 0에서
                     송금하는 시뮬레이터 특성이 있어, 잔액 > 0 조건 없이는 신호가 무의미해진다.
    - DEST_EMPTY:    수취 계좌의 거래 전 잔액이 0이면 1
    - NEW_RECIPIENT: 수취 계좌가 직전 RECIPIENT_WINDOW_MINUTES 동안 입금을 받지 않았으면 1
    """
    if balance_orig_before is None:
        drain = None
    elif balance_orig_before <= 0 or amount > balance_orig_before:
        drain = 0.0
    else:
        drain = amount / balance_orig_before

    return {
        "HIGH_VALUE": amount,
        "HIGH_VALUE_TOP": amount,
        "BALANCE_DRAIN": drain,
        "DEST_EMPTY": None if balance_dest_before is None else float(balance_dest_before == 0),
        "NEW_RECIPIENT": None if recipient_prior_inflow is None else float(recipient_prior_inflow == 0),
        "VELOCITY": velocity,
        "FAILURE_RATE": failure_rate,   # 시스템 신호 — 점수에는 쓰지 않는다
    }


def evaluate_signals(
    signals: Dict[str, Any],
    rules: List[models.FdsRule],
) -> Tuple[float, List[str], List[Dict[str, Any]]]:
    """관측값 → (위험점수, 트리거된 룰, 룰별 기여 내역).

    DB를 모른다. 신호를 어디서 모았는지와 신호를 어떻게 점수로 바꾸는지를 갈라
    놓기 위해서다 — 라이브 경로(evaluate_transaction)와 오프라인 캘리브레이션
    (calibration/)이 **같은 이 함수**를 쓴다. 분석용으로 룰을 다시 구현하면
    "분석에서 근거를 확인한 룰"과 "운영에서 실제로 도는 룰"이 조용히 갈라진다.

    signals: derive_signals의 반환값. 값이 None이거나 키가 없으면 그 룰은 판정하지 않는다(fired=False).
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
    account_to: Optional[str] = None,
    balance_orig_before: Optional[float] = None,
    balance_dest_before: Optional[float] = None,
) -> Dict[str, Any]:
    """DB에서 원천값을 모아 룰 신호를 만든다. 현재 거래는 아직 기록되기 전이다.

    - VELOCITY:      동일 송금 계좌의 VELOCITY_WINDOW_MINUTES 내 거래 건수(현재 건 포함)
    - NEW_RECIPIENT: 수취 계좌가 RECIPIENT_WINDOW_MINUTES 내 받은 입금 건수(현재 건 제외)
    - FAILURE_RATE:  최근 FAILURE_RATE_WINDOW건의 실패율 (시스템 신호, 점수에 쓰지 않음)
    잔액은 호출자가 넘긴다(이체 경로는 잠그지 않고 읽은 거래 전 잔액).

    now: 윈도우의 기준 시각. 운영에서는 비워 둔다(현재 시각). 과거 거래를 같은 정의로
         다시 계산할 때(동등성 테스트) 거래 시각을 넣는다.
    오프라인 평가는 이 함수를 277만 번 부를 수 없어 calibration/dataset.py가 원천값을 따로
    모은다. 두 구현이 같은 값을 내는지는 tests/test_scoring_parity.py가 고정한다.
    """
    now = now or datetime.now(timezone.utc)

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

    recent_count = (
        db.query(models.Transaction)
        .filter(
            models.Transaction.account_from == account_from,
            models.Transaction.created_at >= now - timedelta(minutes=VELOCITY_WINDOW_MINUTES),
        )
        .count()
    )

    recipient_prior_inflow = None
    if account_to is not None:
        recipient_prior_inflow = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.account_to == account_to,
                models.Transaction.created_at >= now - timedelta(minutes=RECIPIENT_WINDOW_MINUTES),
            )
            .count()
        )

    return derive_signals(
        amount,
        balance_orig_before,
        balance_dest_before,
        recipient_prior_inflow,
        float(recent_count + 1),   # 현재 거래를 포함해 센다
        failure_rate,
    )


def evaluate_transaction(
    amount: float,
    account_from: str,
    db: Session,
    rules: List[models.FdsRule],
    account_to: Optional[str] = None,
    balance_orig_before: Optional[float] = None,
    balance_dest_before: Optional[float] = None,
) -> Tuple[float, List[str], List[Dict[str, Any]]]:
    """거래에 대한 (위험점수, 트리거된 룰 유형 목록, 룰별 기여 내역).

    contributions: [{"rule_type", "fired", "weight", "threshold", "actual_value"}, ...]
    LOGIN_FAILURE / LATENCY는 auth.py / 미들웨어에서 별도 처리한다.
    """
    signals = collect_signals(
        amount, account_from, db,
        account_to=account_to,
        balance_orig_before=balance_orig_before,
        balance_dest_before=balance_dest_before,
    )
    return evaluate_signals(signals, rules)


def risk_level(score: float) -> str:
    if score >= RISK_LEVEL_HIGH:
        return "HIGH"
    if score >= RISK_LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"
