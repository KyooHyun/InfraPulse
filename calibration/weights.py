"""거래 룰의 임계값·가중치를 데이터로 정한다 — app/fds_engine.py DEFAULT_RULES의 근거.

    python -m calibration.weights --source paysim --csv evaluation/data/PS_...log.csv --until-step 216

규율: **학습 구간(--until-step 이하)만 본다.** 룰은 레이블을 보고 만드는 것이라 사실상
지도학습이다. 이 스크립트로 정한 값은 10~12일(검증)에서 비교하고, 13일 이후(평가)는
마지막에 한 번만 연다(evaluation/README.md).

  임계값  HIGH_VALUE = 학습 구간 금액의 95 분위수, HIGH_VALUE_TOP = 99 분위수.
          나머지 룰의 임계값은 정의에 따른 값(지표 신호 1.0, 인출 비율 0.9, VELOCITY 5건)이다.
  가중치  points = 10·ln(lift)를 5점 단위로 반올림, lift <= 1이면 0.
          lift가 곱해질 때 점수가 더해지는 로그 오즈 척도라, 룰 여러 개가 함께 발화하면
          점수 합이 결합 lift의 근사가 된다(룰 사이의 상관은 무시 — 나이브 베이즈 가정).
          40점 ≈ lift 55배, 70점 ≈ lift 1,100배.
  HIGH_VALUE_TOP은 HIGH_VALUE에 **더하는** 단계라, 가중치는 (상위 1% lift의 점수 −
          상위 5% lift의 점수)다.
"""
from __future__ import annotations

import argparse
import math
from typing import Dict, List

from app.fds_engine import TRANSACTION_SCOPED_RULES, evaluate_signals
from calibration.console import use_utf8_console
from calibration.dataset import Sample, add_source_arguments, describe, load_samples
from calibration.metrics import percentile
from calibration.rules import default_rules


def points(lift: float) -> float:
    """10·ln(lift)를 5점 단위로. 정보가 없거나(lift <= 1) 반대 방향이면 0."""
    if lift <= 1.0:
        return 0.0
    return float(5 * round(10 * math.log(lift) / 5))


def rule_lifts(samples: List[Sample], overrides: Dict[str, Dict[str, float]]) -> Dict[str, dict]:
    rules = default_rules(overrides)
    base = sum(s.label for s in samples) / len(samples)
    fired = {r: [0, 0] for r in TRANSACTION_SCOPED_RULES}   # [발화 수, 그중 사기]
    for s in samples:
        _, triggered, _ = evaluate_signals(s.signals, rules)
        for rule in triggered:
            fired[rule][0] += 1
            fired[rule][1] += s.label
    out = {}
    for rule, (n, bad) in fired.items():
        precision = bad / n if n else 0.0
        out[rule] = {"support": n / len(samples), "recall": bad / max(1, sum(s.label for s in samples)),
                     "precision": precision, "lift": precision / base if base else 0.0}
    return out


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="거래 룰 임계값·가중치 캘리브레이션")
    add_source_arguments(parser)
    parser.add_argument("--until-step", type=int, default=216, help="학습 구간 끝 step (기본 216 = 9일)")
    args = parser.parse_args()

    samples = [s for s in load_samples(args.source, args.csv, args.limit)
               if s.step is None or s.step <= args.until_step]
    print(describe(samples))

    amounts = [s.amount for s in samples]
    p95, p99 = round(percentile(amounts, 95), 0), round(percentile(amounts, 99), 0)
    overrides = {"HIGH_VALUE": {"threshold": p95}, "HIGH_VALUE_TOP": {"threshold": p99}}
    stats = rule_lifts(samples, overrides)

    weights = {rule: points(stat["lift"]) for rule, stat in stats.items()}
    weights["HIGH_VALUE_TOP"] = max(0.0, weights["HIGH_VALUE_TOP"] - weights["HIGH_VALUE"])

    thresholds = {r.condition_type: r.threshold for r in default_rules(overrides)}
    print(f"\n  {'룰':<16}{'임계값':>14}{'발화율':>10}{'재현율':>9}{'정밀도':>10}{'lift':>9}{'가중치':>8}")
    for rule in TRANSACTION_SCOPED_RULES:
        st = stats[rule]
        print(f"  {rule:<16}{thresholds[rule]:>14,.2f}{st['support']:>10.3%}{st['recall']:>9.3f}"
              f"{st['precision']:>10.4f}{st['lift']:>9.2f}{weights[rule]:>8.0f}")
    print("\n  * 발화율 0 또는 lift <= 1인 룰은 이 데이터에서 근거가 없다는 뜻이다 (가중치 0이 아니라 '측정 불가'일 수 있음).")
    print("\nDEFAULT_RULES에 반영할 값:")
    for rule in TRANSACTION_SCOPED_RULES:
        print(f"  {rule:<16} threshold={thresholds[rule]:,.2f}  weight={weights[rule]:.0f}")


if __name__ == "__main__":
    main()
