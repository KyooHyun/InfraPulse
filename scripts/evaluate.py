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

from app.alerting import budget_threshold, rolling_thresholds
from app.fds_engine import RISK_LEVEL_HIGH, RISK_LEVEL_MEDIUM, evaluate_signals
from app.ml.ensemble import RULE_ALPHA, ensemble_score
from app.ml.isolation_forest import IFModel
from calibration.console import use_utf8_console
from calibration.dataset import Sample, add_source_arguments, describe, load_samples, ml_feature_matrix
from calibration.metrics import average_precision, recall_at_budget, roc_auc
from calibration.rules import default_rules

BUDGETS = (0.001, 0.005, 0.01)
SEED = 42   # --seed로 바꾼다. IF는 무작위 표본으로 트리를 만들어 시드에 따라 결과가 흔들린다.

RULE = "룰"
RULE_ND = "룰 (DRAIN 제외)"
IF = "Isolation Forest"
ENS = f"앙상블 (α={RULE_ALPHA})"
ENS_ND = "앙상블 (DRAIN 제외)"
# BALANCE_DRAIN을 뺀 변형을 항상 함께 낸다. 이 룰은 PaySim에서 합성 데이터의 특성(사기는 잔액을
# 정확히 비운다)에 기대므로, 성능이 이 룰 하나에 기대고 있지 않은지 보여야 한다.
WITHOUT_DRAIN = ("BALANCE_DRAIN",)


def _contamination(labels: Sequence[int]) -> float:
    return float(max(0.001, min(0.5, float(np.mean(labels)))))


def _train_if(samples: List[Sample]) -> IFModel:
    model = IFModel()
    model.fit(ml_feature_matrix(samples), contamination=_contamination([s.label for s in samples]),
              random_state=SEED)
    return model


def _scores(samples: List[Sample], model: Optional[IFModel]):
    rules, rules_nd = default_rules(), default_rules(exclude=WITHOUT_DRAIN)
    rule = [evaluate_signals(s.signals, rules)[0] for s in samples]
    rule_nd = [evaluate_signals(s.signals, rules_nd)[0] for s in samples]
    if model is None:
        return {RULE: rule, RULE_ND: rule_nd}
    if_scores = model.anomaly_scores(ml_feature_matrix(samples)).tolist()
    return {
        RULE: rule,
        RULE_ND: rule_nd,
        IF: if_scores,
        ENS: [ensemble_score(r, i) for r, i in zip(rule, if_scores)],
        ENS_ND: [ensemble_score(r, i) for r, i in zip(rule_nd, if_scores)],
    }


def _level_table(scored: dict, labels: Sequence[int]) -> None:
    """등급 경계(40/70) 기준 — 0~100 점수를 쓰는 방법만(IF는 0~1이라 제외)."""
    n, n_fraud = len(labels), sum(labels)
    print(f"  {'등급 경계 기준':<20}{'MEDIUM↑ 알림':>12}{'재현율':>8}{'정밀도':>9}{'HIGH 알림':>11}{'재현율':>8}{'정밀도':>9}")
    for name, scores in scored.items():
        if name == IF:
            continue
        cells = ""
        for boundary in (RISK_LEVEL_MEDIUM, RISK_LEVEL_HIGH):
            hits = [y for sc, y in zip(scores, labels) if sc >= boundary]
            precision = sum(hits) / len(hits) if hits else float("nan")
            cells += f"{len(hits) / n:>12.3%}{sum(hits) / n_fraud:>8.3f}{precision:>9.4f}"
        print(f"  {name:<20}{cells}")


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

    print()
    _level_table(scored, labels)


def _time_split(samples: List[Sample], train_until: int, segment_steps: List[int], budget: float,
                windows: List[int]) -> None:
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
    thresholds = {name: budget_threshold(scores, budget) for name, scores in train_scored.items()}
    print(f"\n알림 임계값 = 학습 구간 점수 상위 {budget:.1%} 지점")
    for name, thr in thresholds.items():
        actual = sum(1 for s in train_scored[name] if s >= thr) / len(train)
        print(f"  {name:<20}{thr:>10.4f}   학습 구간 실제 알림 비율 {actual:.3%}")

    for i, seg in enumerate(segments, 1):
        labels = [s.label for s in seg]
        base = sum(labels) / len(labels)
        print(f"\n[평가{i}] 사기 비율 {base:.3%} (= 무작위 탐지기의 PR-AUC)")
        print(f"  {'방법':<20}{'ROC-AUC':>9}{'PR-AUC':>9}{'알림비율':>10}{'재현율':>9}{'정밀도':>9}")
        seg_scored = _scores(seg, model_train)
        for name, scores in seg_scored.items():
            thr = thresholds[name]
            flagged = [y for s, y in zip(scores, labels) if s >= thr]
            alert_rate = len(flagged) / len(seg)
            recall = sum(flagged) / sum(labels) if sum(labels) else float("nan")
            precision = sum(flagged) / len(flagged) if flagged else float("nan")
            print(f"  {name:<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}"
                  f"{alert_rate:>10.3%}{recall:>9.3f}{precision:>9.4f}")
        in_sample = _scores(seg, model_all)
        for name in (IF, ENS):
            scores = in_sample[name]
            print(f"  {name + ' *':<20}{roc_auc(scores, labels):>9.3f}{average_precision(scores, labels):>9.4f}"
                  f"{'':>10}{'':>9}{'':>9}")
        _level_table(seg_scored, labels)
    print("\n  * 같은 평가 행을 전체 데이터로 학습한 IF로 채점한 값(표본 내). 위 행과의 차이가 표본 내 평가의 부풀림이다.")

    if windows:
        _drift_report(samples, segments, model_train, thresholds, budget, windows)


def _day(step: int) -> int:
    return (step - 1) // 24 + 1


def _drift_report(samples: List[Sample], segments: List[List[Sample]], model: IFModel,
                  fixed: dict, budget: float, windows: List[int]) -> None:
    """고정 임계값 vs 직전 N일 점수 분위수로 매일 다시 정한 임계값 — 평가 구간의 알림 비율 비교."""
    scored_all = _scores(samples, model)
    days_of = [_day(s.step) for s in samples]
    print(f"\n알림 임계값 갱신 방식별 알림 비율 (예산 {budget:.1%}, 레이블 없이 계산)")
    print("  일별 편차 = 날짜별 알림 비율과 예산의 차이의 평균 |rate_d - 예산|")
    seg_ids = [{id(s) for s in seg} for seg in segments]
    for name in (IF, ENS):
        scores = scored_all[name]
        by_day: dict = {}
        for day, score in zip(days_of, scores):
            by_day.setdefault(day, []).append(score)
        print(f"\n  [{name}]")
        print(f"  {'구간':<6}{'방식':<14}{'알림비율':>10}{'일별 편차':>10}{'최대 일':>10}{'재현율':>9}{'정밀도':>9}")
        for seg_no, ids in enumerate(seg_ids, 1):
            rows = [i for i, s in enumerate(samples) if id(s) in ids]
            days = sorted({days_of[i] for i in rows})
            schemes = [("고정(학습)", {d: fixed[name] for d in days})]
            schemes += [(f"직전 {w}일", rolling_thresholds(by_day, days, w, budget)) for w in windows]
            for label, thr in schemes:
                flagged = caught = n_fraud = 0
                daily: dict = {}
                for i in rows:
                    hit = scores[i] >= thr[days_of[i]]
                    label_i = samples[i].label
                    flagged += hit
                    caught += hit and label_i
                    n_fraud += label_i
                    n, f = daily.get(days_of[i], (0, 0))
                    daily[days_of[i]] = (n + 1, f + hit)
                rates = [f / n for n, f in daily.values()]
                dev = sum(abs(r - budget) for r in rates) / len(rates)
                precision = caught / flagged if flagged else float("nan")
                print(f"  평가{seg_no:<3}{label:<14}{flagged / len(rows):>10.3%}{dev:>10.3%}{max(rates):>10.3%}"
                      f"{caught / n_fraud:>9.3f}{precision:>9.4f}")


def _seed_summary(samples: List[Sample], train_until: int, segment_steps: List[int], budget: float,
                  seeds: List[int]) -> None:
    """시드 여러 개로 IF를 다시 학습해 평균 ± 표준편차를 낸다. 룰은 시드와 무관하다.

    비교 지표:
      ROC-AUC, PR-AUC
      재현율@예산 — 평가 구간 안에서 상위 budget 비율만 알림으로 보낼 때(모든 방법이 같은 알림 건수)
      알림비율·재현율·정밀도 — 학습 구간에서 정한 임계값을 그대로 적용했을 때(운영과 같은 방식)
    """
    global SEED
    train = [s for s in samples if s.step <= train_until]
    bounds = [train_until] + sorted(segment_steps) + [max(s.step for s in samples)]
    segments = [[s for s in samples if lo < s.step <= hi] for lo, hi in zip(bounds, bounds[1:])]
    segments = [seg for seg in segments if seg]
    print(f"\n학습   {_segment_label(train)}")
    for i, seg in enumerate(segments, 1):
        print(f"평가{i}  {_segment_label(seg)}")

    runs: dict = {}   # (seg, method) -> list of metric tuples
    for seed in seeds:
        SEED = seed
        print(f"  시드 {seed} ...", flush=True)
        model = _train_if(train)
        train_scored = _scores(train, model)
        thresholds = {name: budget_threshold(sc, budget) for name, sc in train_scored.items()}
        for i, seg in enumerate(segments, 1):
            labels = [s.label for s in seg]
            for name, scores in _scores(seg, model).items():
                hits = [y for sc, y in zip(scores, labels) if sc >= thresholds[name]]
                runs.setdefault((i, name), []).append((
                    roc_auc(scores, labels), average_precision(scores, labels),
                    recall_at_budget(scores, labels, budget),
                    len(hits) / len(seg), sum(hits) / sum(labels),
                    sum(hits) / len(hits) if hits else float("nan"),
                ))

    def cell(values, fmt):
        mean = float(np.mean(values))
        sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        return f"{mean:{fmt}}±{sd:{fmt}}" if sd > 0 else f"{mean:{fmt}}"

    for i, seg in enumerate(segments, 1):
        base = sum(s.label for s in seg) / len(seg)
        print(f"\n[평가{i}] 사기 비율 {base:.3%}  (시드 {len(seeds)}개 평균±표준편차, 룰은 시드 무관)")
        print(f"  {'방법':<18}{'ROC-AUC':>14}{'PR-AUC':>16}{'재현율@예산':>15}"
              f"{'알림비율':>16}{'재현율':>14}{'정밀도':>16}")
        for name in (RULE, RULE_ND, IF, ENS, ENS_ND):
            m = list(zip(*runs[(i, name)]))
            print(f"  {name:<18}{cell(m[0], '.3f'):>14}{cell(m[1], '.4f'):>16}{cell(m[2], '.3f'):>15}"
                  f"{cell([v * 100 for v in m[3]], '.3f') + '%':>16}{cell(m[4], '.3f'):>14}{cell(m[5], '.4f'):>16}")


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="운영 엔진으로 잰 룰/IF/앙상블 비교")
    add_source_arguments(parser)
    parser.add_argument("--train-until-step", type=int, default=None,
                        help="이 step까지를 학습 구간으로 쓴다 (없으면 표본 내 평가)")
    parser.add_argument("--segment-steps", type=int, nargs="*", default=[],
                        help="평가 구간을 나눌 step 경계 (예: 408 → 학습 이후~408, 409~끝)")
    parser.add_argument("--budget", type=float, default=0.005, help="알림 예산 비율 (기본 0.005)")
    parser.add_argument("--max-step", type=int, default=None,
                        help="이 step 이후의 거래는 읽지 않는다 (검증 단계에서 평가 구간을 열지 않기 위해)")
    parser.add_argument("--seed", type=int, default=42, help="IF 시드")
    parser.add_argument("--seeds", type=int, nargs="*", default=[],
                        help="시드 여러 개로 반복해 평균±표준편차 요약표만 낸다 (예: 0 1 2 3 4)")
    parser.add_argument("--rolling-days", type=int, nargs="*", default=[],
                        help="직전 N일 점수 분위수로 매일 임계값을 다시 정하는 방식도 비교 (예: 1 3 7)")
    args = parser.parse_args()

    global SEED
    SEED = args.seed
    samples = load_samples(args.source, args.csv, args.limit)
    if args.max_step is not None:
        samples = [s for s in samples if s.step is not None and s.step <= args.max_step]
    print("=" * 78)
    print(describe(samples))
    print("=" * 78)

    if args.train_until_step is None:
        _in_sample(samples)
        return
    if any(s.step is None for s in samples):
        raise SystemExit("시간 분할에는 step이 있는 출처(--source paysim)가 필요하다")
    if args.seeds:
        _seed_summary(samples, args.train_until_step, args.segment_steps, args.budget, args.seeds)
        return
    _time_split(samples, args.train_until_step, args.segment_steps, args.budget, args.rolling_days)


if __name__ == "__main__":
    main()
