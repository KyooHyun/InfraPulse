#!/usr/bin/env python
"""룰 단독 vs Isolation Forest vs 앙상블 — 운영 엔진의 점수 함수로 잰다.

Usage:
    # 시간 분할 (권장): 1~12일로 학습·임계값 결정, 13~17일 / 18~31일에서 평가
    python scripts/evaluate.py --source paysim --csv evaluation/data/PS_...log.csv --train-until-step 288 --segment-steps 408

    # 표본 내 평가 (같은 데이터로 학습·평가 — 낙관적)
    python scripts/evaluate.py --source paysim --csv evaluation/data/PS_...log.csv

이 스크립트는 점수를 **계산하지 않는다**. 전부 운영 코드를 import해서 쓴다:
  룰 점수   app.fds_engine.evaluate_signals + app.fds_engine.DEFAULT_RULES (calibration.rules)
  IF 점수   app.ml.isolation_forest.IFModel (학습·정규화 포함)
  앙상블    app.ml.ensemble.ensemble_score (α = RULE_ALPHA)
  신호·피처 calibration.dataset — 운영 경로와의 일치는 tests/test_scoring_parity.py가 고정한다

시간 분할 모드에서 하는 일:
  1. 학습 구간(step <= --train-until-step)으로만 IF를 학습한다.
  2. 알림 임계값을 학습 구간 점수의 상위 --budget(기본 0.5%) 지점으로 정한다.
  3. 평가 구간별로 ROC-AUC, PR-AUC, 그리고 그 임계값을 그대로 썼을 때의 **실제 알림 비율**,
     재현율, 정밀도를 낸다. 실제 알림 비율이 예산에서 벗어난 정도가 분포 이동(드리프트)의 근거다.
  4. 같은 평가 행을 "전체 데이터로 학습한 IF"로도 채점해, 표본 내 평가가 얼마나 부풀렸는지 보인다.

PR-AUC는 사기 비율에 따라 기준선이 달라지므로(무작위 탐지기의 PR-AUC = 사기 비율) 구간별
사기 비율을 함께 출력한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.fds_engine import RISK_LEVEL_HIGH, RISK_LEVEL_MEDIUM, evaluate_signals
from app.ml.ensemble import RULE_ALPHA, ensemble_score
from app.ml.isolation_forest import IFModel
from calibration.console import use_utf8_console
from calibration.dataset import Sample, add_source_arguments, describe, load_samples, ml_feature_matrix
from calibration.metrics import average_precision, recall_at_budget, roc_auc
from calibration.rules import default_rules

BUDGETS = (0.001, 0.005, 0.01)


def _contamination(labels: Sequence[int]) -> float:
    return float(max(0.001, min(0.5, float(np.mean(labels)))))


def _train_if(samples: List[Sample]) -> IFModel:
    model = IFModel()
    model.fit(ml_feature_matrix(samples), contamination=_contamination([s.label for s in samples]))
    return model


def _scores(samples: List[Sample], model: Optional[IFModel]):
    rules = default_rules()
    rule = [evaluate_signals(s.signals, rules)[0] for s in samples]
    if model is None:
        return {"룰 단독": rule}
    if_scores = model.anomaly_scores(ml_feature_matrix(samples)).tolist()
    return {
        "룰 단독": rule,
        "Isolation Forest": if_scores,
        f"앙상블 (α={RULE_ALPHA})": [ensemble_score(r, i) for r, i in zip(rule, if_scores)],
    }


def _budget_threshold(scores: Sequence[float], budget: float) -> float:
    """점수 상위 budget 비율이 되는 지점. 동점이 많으면 실제 알림 비율은 예산보다 커진다."""
    ordered = sorted(scores, reverse=True)
    return ordered[max(int(budget * len(ordered)) - 1, 0)]


def _segment_label(samples: List[Sample]) -> str:
    steps = [s.step for s in samples]
    fraud = sum(s.label for s in samples)
    return (f"step {min(steps)}~{max(steps)} (일 {(min(steps) - 1) // 24 + 1}~{(max(steps) - 1) // 24 + 1})  "
            f"{len(samples):,}건  사기 {fraud:,}건 = {fraud / len(samples):.3%}")


def _in_sample(samples: List[Sample]) -> None:
    labels = [s.label for s in samples]
    model = IFModel.load()
    if model is None:
        print("  IF 모델 없음 — 룰 단독만 출력 (python scripts/train_model.py 먼저 실행)")
    header = "  ".join(f"recall@{b:.1%}".rjust(9) for b in BUDGETS)
    print(f"\n  {'방법':<20}{'ROC-AUC':>9}{'PR-AUC':>9}  {header}")
    scored = _scores(samples, model)
    for name, scores in scored.items():
        budgets = "  ".join(f"{recall_at_budget(scores, labels, b):>9.3f}" for b in BUDGETS)
        print(f"  {name:<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}  {budgets}")
    print("  * 표본 내 평가(같은 데이터로 학습·평가) — 낙관적인 값이다")

    print(f"\n현행 등급 경계({RISK_LEVEL_MEDIUM:.0f}/{RISK_LEVEL_HIGH:.0f})에 걸리는 거래")
    for name, scores in scored.items():
        if name == "Isolation Forest":
            continue
        medium = sum(1 for s in scores if RISK_LEVEL_MEDIUM <= s < RISK_LEVEL_HIGH)
        high = sum(1 for s in scores if s >= RISK_LEVEL_HIGH)
        print(f"  {name:<20}MEDIUM {medium:>10,}건   HIGH {high:>10,}건   최고점 {max(scores):.2f}")


def _time_split(samples: List[Sample], train_until: int, segment_steps: List[int], budget: float) -> None:
    train = [s for s in samples if s.step <= train_until]
    bounds = [train_until] + sorted(segment_steps) + [max(s.step for s in samples)]
    segments = [[s for s in samples if lo < s.step <= hi] for lo, hi in zip(bounds, bounds[1:])]
    segments = [seg for seg in segments if seg]

    print(f"\n학습   {_segment_label(train)}")
    for i, seg in enumerate(segments, 1):
        print(f"평가{i}  {_segment_label(seg)}")

    print("\nIF 학습: 학습 구간만 / 전체(표본 내 대조군)")
    model_train = _train_if(train)
    model_all = _train_if(samples)

    train_scored = _scores(train, model_train)
    thresholds = {name: _budget_threshold(scores, budget) for name, scores in train_scored.items()}
    print(f"\n알림 임계값 = 학습 구간 점수 상위 {budget:.1%} 지점")
    for name, thr in thresholds.items():
        actual = sum(1 for s in train_scored[name] if s >= thr) / len(train)
        print(f"  {name:<20}{thr:>10.4f}   학습 구간 실제 알림 비율 {actual:.3%}")

    for i, seg in enumerate(segments, 1):
        labels = [s.label for s in seg]
        base = sum(labels) / len(labels)
        print(f"\n[평가{i}] 사기 비율 {base:.3%} (= 무작위 탐지기의 PR-AUC)")
        print(f"  {'방법':<20}{'ROC-AUC':>9}{'PR-AUC':>9}{'알림비율':>10}{'재현율':>9}{'정밀도':>9}")
        for name, scores in _scores(seg, model_train).items():
            thr = thresholds[name]
            flagged = [y for s, y in zip(scores, labels) if s >= thr]
            alert_rate = len(flagged) / len(seg)
            recall = sum(flagged) / sum(labels) if sum(labels) else float("nan")
            precision = sum(flagged) / len(flagged) if flagged else float("nan")
            print(f"  {name:<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}"
                  f"{alert_rate:>10.3%}{recall:>9.3f}{precision:>9.4f}")
        in_sample = _scores(seg, model_all)
        for name in ("Isolation Forest", f"앙상블 (α={RULE_ALPHA})"):
            scores = in_sample[name]
            print(f"  {name + ' *':<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}"
                  f"{'':>10}{'':>9}{'':>9}")
    print("\n  * 같은 평가 행을 전체 데이터로 학습한 IF로 채점한 값(표본 내). 위 행과의 차이가 표본 내 평가의 부풀림이다.")


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="운영 엔진으로 잰 룰/IF/앙상블 비교")
    add_source_arguments(parser)
    parser.add_argument("--train-until-step", type=int, default=None,
                        help="이 step까지를 학습 구간으로 쓴다 (없으면 표본 내 평가)")
    parser.add_argument("--segment-steps", type=int, nargs="*", default=[],
                        help="평가 구간을 나눌 step 경계 (예: 408 → 학습 이후~408, 409~끝)")
    parser.add_argument("--budget", type=float, default=0.005, help="알림 예산 비율 (기본 0.005)")
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    print("=" * 78)
    print(describe(samples))
    print("=" * 78)

    if args.train_until_step is None:
        _in_sample(samples)
        return
    if any(s.step is None for s in samples):
        raise SystemExit("시간 분할에는 step이 있는 출처(--source paysim)가 필요하다")
    _time_split(samples, args.train_until_step, args.segment_steps, args.budget)


if __name__ == "__main__":
    main()
