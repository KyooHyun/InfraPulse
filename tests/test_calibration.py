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
    signals = {"HIGH_VALUE": 500_000.0, "FAILURE_RATE": None, "VELOCITY": None}
    score, triggered, contributions = evaluate_signals(signals, rules)

    assert triggered == ["HIGH_VALUE"]
    assert score == 30.0
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


def test_system_level_rules_are_reported_as_non_contributing():
    """LOGIN_FAILURE와 LATENCY는 fds_rules에 가중치를 달고 있지만 거래 점수에는 못 들어간다."""
    result = reachability.analyze()
    orphans = {rule["rule_type"] for rule in result["orphan_rules"]}

    assert orphans == {"LOGIN_FAILURE", "LATENCY"}
    assert result["orphan_weight_total"] > 0
    assert result["declared_weight_total"] > result["max_reachable_rule_score"]


def test_level_reachability_flags_match_the_arithmetic():
    result = reachability.analyze()
    maximum = result["max_reachable_rule_score"]

    for level in result["levels"]:
        assert level["reachable_by_rules"] == (maximum >= level["boundary"])


def test_known_gap_rule_only_config_cannot_reach_high():
    """**현재 알려진 결함을 고정한다.**

    거래 단위 룰 가중치의 합(30+25+10=65)이 HIGH 경계(70)보다 작다. 즉 ML을 끈
    구성에서는 어떤 거래도 HIGH가 될 수 없고, 그 경계에 걸린 STR 초안 생성은
    실행되지 않는다. README의 "위험점수 70점 이상 거래에 STR 초안 생성"은
    룰 전용 구성에서는 사실이 아니다.

    임계값을 내릴지 가중치를 올릴지는 데이터가 정할 일이라 여기서 손대지 않았다
    (calibration/threshold.py). **이 테스트가 실패하면 그 결정이 내려졌다는 뜻이므로,
    README와 calibration/README.md 를 함께 고쳐야 한다.**
    """
    result = reachability.analyze()
    high = next(level for level in result["levels"] if level["level"] == "HIGH")

    assert result["max_reachable_rule_score"] < RISK_LEVEL_HIGH
    assert high["reachable_by_rules"] is False
    assert high["shortfall"] == pytest.approx(5.0)


def test_medium_is_unreachable_without_high_value():
    """HIGH_VALUE가 발화하지 않으면 나머지 룰을 다 합쳐도 MEDIUM에 못 미친다.

    FAILURE_RATE(25) + VELOCITY(10) = 35 < 40. 고액이 아닌 이상거래는 아무리
    여러 룰에 걸려도 담당자 검토 대기열에 올라오지 않는다는 뜻이다.
    """
    result = reachability.analyze()
    without_high_value = [
        row for row in result["reachable_score_table"]
        if "HIGH_VALUE" not in row["fired_rules"]
    ]

    assert max(row["score"] for row in without_high_value) < RISK_LEVEL_MEDIUM


def test_ensemble_configuration_can_reach_high():
    """ML을 켜면 HIGH에 도달할 수 있다 — 같은 임계값이 두 구성에서 다른 의미를 갖는다."""
    ensemble = reachability.analyze()["ensemble"]

    assert ensemble["high_reachable"] is True
    assert ensemble["max_reachable_score"] >= RISK_LEVEL_HIGH
