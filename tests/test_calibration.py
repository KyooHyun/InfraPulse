"""캘리브레이션 도구 테스트.

라벨 붙은 데이터셋 없이도 돌아가는 것만 모았다. 데이터가 필요한 부분
(rule_lift / threshold 의 실제 수치)은 데이터셋을 받은 자리에서 CLI로 확인한다 —
calibration/README.md 참고.

여기서 고정하는 것은 두 가지다.
  · 지표 구현이 정의대로인가 (손으로 계산한 값과 맞춰본다)
  · 신호 계산에 미래 정보가 새지 않는가 (look-ahead 방지)
"""
import math

import pytest

from app.fds_engine import (
    RISK_LEVEL_HIGH,
    RISK_LEVEL_MEDIUM,
    TRANSACTION_SCOPED_RULES,
    evaluate_signals,
)
from calibration import reachability
from calibration.dataset import _rolling_failure_rate, _velocity_by_window
from calibration.metrics import confusion_at, lift, percentile, roc_auc
from calibration.rules import default_rules, with_threshold


# ── 지표 ─────────────────────────────────────────────────────────────────────

def test_confusion_at_counts_each_quadrant():
    scores = [10, 50, 50, 90]
    labels = [0, 1, 0, 1]
    c = confusion_at(scores, labels, 50)

    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 0, 1)
    assert c.precision == pytest.approx(2 / 3)
    assert c.recall == 1.0
    assert c.fpr == pytest.approx(0.5)


def test_roc_auc_perfect_and_inverted():
    assert roc_auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0
    assert roc_auc([1, 2, 3, 4], [1, 1, 0, 0]) == 0.0


def test_roc_auc_counts_ties_as_half():
    """룰 점수는 값이 몇 개뿐이라 동점이 대량으로 생긴다.

    동점을 얼버무리면 AUC가 실제보다 좋게 나오므로 정의(동점=0.5)를 고정해 둔다.
    """
    assert roc_auc([5, 5], [0, 1]) == 0.5
    assert roc_auc([5, 5, 5, 5], [0, 0, 1, 1]) == 0.5


def test_roc_auc_is_nan_without_both_classes():
    assert math.isnan(roc_auc([1, 2, 3], [0, 0, 0]))


def test_lift_is_one_when_rule_carries_no_information():
    assert lift(0.05, 0.05) == 1.0


def test_percentile_interpolates():
    assert percentile([0, 10], 50) == pytest.approx(5.0)
    assert percentile([1, 2, 3, 4, 5], 100) == 5


# ── 신호 계산: 미래 정보가 새지 않는가 ────────────────────────────────────────

def test_velocity_counts_only_the_past():
    """VELOCITY는 현재 시점까지만 센다.

    전체 데이터를 보고 세면 "나중에 몰아친 거래"가 지금 건의 신호로 들어가
    (look-ahead) lift가 실제보다 좋게 나온다. 캘리브레이션에서 이 실수를 하면
    근거가 아니라 착시가 된다.
    """
    rows = [
        {"account": "A", "minutes": 0.0},
        {"account": "A", "minutes": 1.0},
        {"account": "A", "minutes": 2.0},
        {"account": "B", "minutes": 2.0},
    ]
    assert _velocity_by_window(rows, "account", "minutes", window=10) == [1.0, 2.0, 3.0, 1.0]


def test_velocity_drops_events_outside_the_window():
    rows = [
        {"account": "A", "minutes": 0.0},
        {"account": "A", "minutes": 5.0},
        {"account": "A", "minutes": 100.0},   # 앞의 둘은 창 밖으로 나간다
    ]
    assert _velocity_by_window(rows, "account", "minutes", window=10) == [1.0, 2.0, 1.0]


def test_failure_rate_is_none_until_window_fills():
    """표본이 모자란 구간은 0이 아니라 None이어야 한다.

    0으로 채우면 "실패율 0%가 관측됐다"는 뜻이 되어, 룰이 발화하지 않은 이유가
    '조건 미달'인지 '판정 불가'인지 구분되지 않는다.
    """
    rates = _rolling_failure_rate(["success"] * 10)
    assert all(rate is None for rate in rates)


def test_failure_rate_uses_only_preceding_transactions():
    from app.fds_engine import FAILURE_RATE_WINDOW

    statuses = ["failed"] * FAILURE_RATE_WINDOW + ["success"]
    rates = _rolling_failure_rate(statuses)

    assert rates[FAILURE_RATE_WINDOW - 1] is None      # 아직 한 건 모자라다
    assert rates[FAILURE_RATE_WINDOW] == 1.0           # 직전 N건이 전부 실패


# ── 룰 평가: 관측 불가 신호 ───────────────────────────────────────────────────

def test_missing_signal_does_not_fire_the_rule():
    rules = default_rules()
    # HIGH_VALUE_TOP 등 나머지 신호는 키가 없거나 None — 판정하지 않는다
    signals = {"HIGH_VALUE": 1_000_000.0, "BALANCE_DRAIN": None, "VELOCITY": None}
    score, triggered, contributions = evaluate_signals(signals, rules)

    assert triggered == ["HIGH_VALUE"]
    assert score == 20.0
    assert all(c["fired"] is False for c in contributions if c["rule_type"] != "HIGH_VALUE")


def test_threshold_override_changes_firing():
    signals = {"HIGH_VALUE": 50_000.0}
    assert evaluate_signals(signals, default_rules())[1] == []
    assert evaluate_signals(signals, with_threshold("HIGH_VALUE", 10_000.0))[1] == ["HIGH_VALUE"]


# ── 점수 공간 도달 가능성 ─────────────────────────────────────────────────────

def test_max_reachable_score_is_the_sum_of_transaction_scoped_weights():
    result = reachability.analyze()
    weights = result["rule_weights"]
    expected = min(sum(weights[rule] for rule in TRANSACTION_SCOPED_RULES), 100.0)

    assert result["max_reachable_rule_score"] == expected


def test_system_level_rules_carry_no_weight():
    """FAILURE_RATE·LOGIN_FAILURE·LATENCY는 시스템 신호라 거래 점수에 들어가지 않는다.

    예전에는 이 룰들이 가중치(25·20·15)를 달고 있어 "룰 5개 100점"처럼 보였지만, 거래 한 건이
    받을 수 있는 점수에는 보태지 않았다. 이제 가중치 0이라 선언과 실제가 일치한다.
    """
    result = reachability.analyze()
    orphans = {rule["rule_type"] for rule in result["orphan_rules"]}

    assert orphans == {"FAILURE_RATE", "LOGIN_FAILURE", "LATENCY"}
    assert result["orphan_weight_total"] == 0


def test_level_reachability_flags_match_the_arithmetic():
    result = reachability.analyze()
    maximum = result["max_reachable_rule_score"]

    for level in result["levels"]:
        assert level["reachable_by_rules"] == (maximum >= level["boundary"])


def test_rule_only_config_can_reach_high():
    """룰만 쓰는 구성에서도 HIGH(70점)에 도달한다 — STR 초안 생성 경로가 살아 있다.

    예전 가중치(30+25+10=65)로는 HIGH가 구조적으로 불가능했다. calibration/weights.py로 다시 정한
    가중치에서는 도달한다. **이 테스트가 실패하면 HIGH 등급과 STR 초안이 다시 죽은 경로가 된다.**
    """
    result = reachability.analyze()
    high = next(level for level in result["levels"] if level["level"] == "HIGH")

    assert result["max_reachable_rule_score"] >= RISK_LEVEL_HIGH
    assert high["reachable_by_rules"] is True


def test_high_requires_balance_drain():
    """HIGH에 도달하는 모든 룰 조합에 BALANCE_DRAIN이 들어 있다 — 알려진 의존성을 고정한다.

    BALANCE_DRAIN은 PaySim에서 합성 데이터의 특성(사기는 잔액을 정확히 비운다)에 기대는 신호다.
    그래서 평가에서는 이 룰을 뺀 결과를 나란히 보고한다(evaluation/README.md).
    """
    result = reachability.analyze()
    high_rows = [row for row in result["reachable_score_table"] if row["score"] >= RISK_LEVEL_HIGH]

    assert high_rows
    assert all("BALANCE_DRAIN" in row["fired_rules"] for row in high_rows)


def test_medium_is_reachable_without_high_value():
    """고액이 아닌 이상거래도 MEDIUM(검토 대기열)에 오를 수 있다.

    예전에는 HIGH_VALUE 없이는 나머지 룰을 다 합쳐도 35점이라, 소액 분할 수법이 구조적으로
    검토 대상에서 빠졌다.
    """
    result = reachability.analyze()
    without_amount = [
        row for row in result["reachable_score_table"]
        if not {"HIGH_VALUE", "HIGH_VALUE_TOP"} & set(row["fired_rules"])
    ]

    assert max(row["score"] for row in without_amount) >= RISK_LEVEL_MEDIUM


def test_ensemble_configuration_can_reach_high():
    """ML을 켜면 HIGH에 도달할 수 있다 — 같은 임계값이 두 구성에서 다른 의미를 갖는다."""
    ensemble = reachability.analyze()["ensemble"]

    assert ensemble["high_reachable"] is True
    assert ensemble["max_reachable_score"] >= RISK_LEVEL_HIGH
