#!/usr/bin/env python
"""룰 단독 vs Isolation Forest vs 앙상블 — 운영 엔진의 점수 함수로 잰다.

Usage:
    python scripts/evaluate.py --source paysim --csv evaluation/data/PS_20174392719_1491204439457_log.csv
    python scripts/evaluate.py --source db

이 스크립트는 점수를 **계산하지 않는다**. 전부 운영 코드를 import해서 쓴다:
  룰 점수   app.fds_engine.evaluate_signals + app.fds_engine.DEFAULT_RULES (calibration.rules)
  IF 점수   app.ml.isolation_forest.IFModel.anomaly_scores (정규화 포함)
  앙상블    app.ml.ensemble.ensemble_score (α = RULE_ALPHA)
  신호·피처 calibration.dataset — 운영 경로와의 일치는 tests/test_scoring_parity.py가 고정한다

예전 평가 스크립트(evaluation/paysim_eval.py, creditcard_eval.py)는 룰·α·IF 정규화를
스크립트 안에서 따로 구현했고, 그래서 운영 시스템과 다른 것을 쟀다. 그 결과는 철회했다
(evaluation/README.md).

비교 지표:
  - ROC-AUC, PR-AUC (임계값과 무관한 순위 성능)
  - 같은 알림 예산(상위 0.1% / 0.5% / 1%)에서의 재현율 — 임계값만 올려도 FPR은 내려가므로
    두 탐지기는 같은 알림 건수에서 비교해야 한다
  - 현행 등급 경계(40/70)에 걸리는 거래 수

주의: IF는 같은 데이터로 학습·평가한다(표본 내). 시간 분할 평가는 다음 작업이다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fds_engine import RISK_LEVEL_HIGH, RISK_LEVEL_MEDIUM, evaluate_signals
from app.ml.ensemble import RULE_ALPHA, ensemble_score
from app.ml.isolation_forest import IFModel
from calibration.console import use_utf8_console
from calibration.dataset import add_source_arguments, describe, load_samples, ml_feature_matrix
from calibration.metrics import average_precision, recall_at_budget, roc_auc
from calibration.rules import default_rules

BUDGETS = (0.001, 0.005, 0.01)


def _report(name: str, scores, labels) -> None:
    budgets = "  ".join(f"{recall_at_budget(scores, labels, b):>9.3f}" for b in BUDGETS)
    print(f"  {name:<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}  {budgets}")


def _levels(name: str, scores, labels) -> None:
    medium = sum(1 for s in scores if RISK_LEVEL_MEDIUM <= s < RISK_LEVEL_HIGH)
    high = sum(1 for s in scores if s >= RISK_LEVEL_HIGH)
    high_fraud = sum(1 for s, y in zip(scores, labels) if s >= RISK_LEVEL_HIGH and y)
    print(f"  {name:<20}MEDIUM {medium:>10,}건   HIGH {high:>10,}건 (그중 사기 {high_fraud:,})")


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="운영 엔진으로 잰 룰/IF/앙상블 비교")
    add_source_arguments(parser)
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    labels = [s.label for s in samples]
    rules = default_rules()
    rule_scores = [evaluate_signals(s.signals, rules)[0] for s in samples]

    print("=" * 78)
    print(describe(samples))
    print("=" * 78)
    header = "  ".join(f"recall@{b:.1%}".rjust(9) for b in BUDGETS)
    print(f"\n  {'방법':<20}{'ROC-AUC':>9}{'PR-AUC':>9}  {header}")
    _report("룰 단독", rule_scores, labels)

    model = IFModel.load()
    ensemble = None
    if model is None:
        print("\n  IF 모델 없음 — 룰 단독만 출력 (python scripts/train_model.py 먼저 실행)")
    else:
        if_scores = model.anomaly_scores(ml_feature_matrix(samples)).tolist()
        ensemble = [ensemble_score(r, i) for r, i in zip(rule_scores, if_scores)]
        _report("Isolation Forest", if_scores, labels)
        _report(f"앙상블 (α={RULE_ALPHA})", ensemble, labels)
        print("  * IF는 표본 내 평가(같은 데이터로 학습) — 낙관적인 값이다")

    print(f"\n현행 등급 경계({RISK_LEVEL_MEDIUM:.0f}/{RISK_LEVEL_HIGH:.0f})에 걸리는 거래")
    _levels("룰 단독", rule_scores, labels)
    if ensemble is not None:
        _levels(f"앙상블 (α={RULE_ALPHA})", ensemble, labels)


if __name__ == "__main__":
    main()
