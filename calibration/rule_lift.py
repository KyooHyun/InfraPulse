"""룰별 예측력과 임계값 근거 — "왜 10만원인가"에 답하는 자리.

app/fds_engine.py 의 기본 임계값(10만원, 실패율 30%, 10분 5건)과 가중치
(30/25/20/15/10)는 금융 상식으로 정한 판단치다. "고액거래가 고빈도거래보다
위험하다"는 방향은 맞을 수 있어도, 그 배수가 왜 3배여야 하는지는 근거가 없었다.

이 스크립트는 세 가지를 데이터로 확인한다.

  1) 룰별 lift = P(사기 | 룰 탐지) / P(사기)
     탐지된 거래가 실제로 얼마나 더 사기인가. 1.0이면 아무 정보가 없는 룰이다.
     수기 가중치 순위와 lift 순위가 어긋나면 가중치 배분이 틀린 것이다.

  2) 임계값 곡선
     임계값을 후보값들로 옮겨가며 lift와 탐지량을 잰다. "10만원"이 그 곡선에서
     어디쯤인지 보이면 비로소 그 숫자를 설명할 수 있다.

  3) 룰 간 중복
     거의 항상 같이 뜨는 룰은 점수를 두 번 더한다. 각각 30점·10점이 사실상
     40점짜리 단일 신호였다는 뜻이 된다.

실행:
    python -m calibration.rule_lift --source db
    python -m calibration.rule_lift --source paysim --csv evaluation/data/PS_...log.csv --limit 300000

산출: calibration/outputs/rule_lift.csv, rule_threshold_curve.csv, rule_overlap.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fds_engine import TRANSACTION_SCOPED_RULES, evaluate_signals
from calibration.console import use_utf8_console
from calibration.dataset import Sample, add_source_arguments, describe, load_samples
from calibration.metrics import lift, percentile
from calibration.rules import default_rules, with_threshold

OUT = Path(__file__).resolve().parent / "outputs"

# 임계값 후보로 훑을 분위수. 꼬리 쪽을 촘촘히 본다 — 고액거래 룰의 관심사는
# 분포의 오른쪽 끝이고, 중앙값 근처를 아무리 잘게 나눠도 판단이 달라지지 않는다.
THRESHOLD_PERCENTILES = (50, 75, 90, 95, 97.5, 99, 99.5, 99.9)


def base_rate(samples: List[Sample]) -> float:
    return sum(s.label for s in samples) / len(samples) if samples else 0.0


def rule_stats(samples: List[Sample], rules) -> List[Dict[str, Any]]:
    """현재 임계값에서의 룰별 정밀도·재현율·lift."""
    rate = base_rate(samples)
    positives = sum(s.label for s in samples)
    rule_map = {r.condition_type: r for r in rules}

    fired_by_rule: Dict[str, List[Sample]] = {rule: [] for rule in TRANSACTION_SCOPED_RULES}
    for sample in samples:
        _, triggered, _ = evaluate_signals(sample.signals, rules)
        for rule_type in triggered:
            fired_by_rule[rule_type].append(sample)

    rows = []
    for rule_type in TRANSACTION_SCOPED_RULES:
        rule = rule_map.get(rule_type)
        if rule is None:
            continue
        observable = [s for s in samples if s.signals.get(rule_type) is not None]
        detected = fired_by_rule[rule_type]
        hits = sum(s.label for s in detected)
        precision = hits / len(detected) if detected else 0.0
        rows.append({
            "rule_type": rule_type,
            "hand_weight": rule.weight,
            "threshold": rule.threshold,
            "observable": len(observable),
            "support": len(detected),
            "support_rate": round(len(detected) / len(observable), 4) if observable else None,
            "fraud_hits": hits,
            "precision": round(precision, 6),
            "recall": round(hits / positives, 4) if positives else 0.0,
            "lift": round(lift(precision, rate), 3),
        })
    return sorted(rows, key=lambda row: -row["lift"])


def threshold_curve(samples: List[Sample], rule_type: str) -> List[Dict[str, Any]]:
    """임계값 후보별 lift·탐지량. 현재 임계값이 이 곡선 어디에 있는지가 근거가 된다."""
    observed = [s.signals[rule_type] for s in samples if s.signals.get(rule_type) is not None]
    if not observed:
        return []

    rate = base_rate(samples)
    positives = sum(s.label for s in samples)
    current = {r.condition_type: r.threshold for r in default_rules()}[rule_type]

    candidates = sorted({
        round(percentile(observed, q), 2) for q in THRESHOLD_PERCENTILES
    } | {current})

    rows = []
    for candidate in candidates:
        rules = with_threshold(rule_type, candidate)
        detected = [
            s for s in samples
            if rule_type in evaluate_signals(s.signals, rules)[1]
        ]
        hits = sum(s.label for s in detected)
        precision = hits / len(detected) if detected else 0.0
        rows.append({
            "rule_type": rule_type,
            "threshold": candidate,
            "is_current": candidate == current,
            "support": len(detected),
            "support_rate": round(len(detected) / len(samples), 4),
            "fraud_hits": hits,
            "precision": round(precision, 6),
            "recall": round(hits / positives, 4) if positives else 0.0,
            "lift": round(lift(precision, rate), 3),
        })
    return rows


def overlap_stats(samples: List[Sample], rules) -> List[Dict[str, Any]]:
    """룰 쌍의 동시 발화 정도. 자카드가 높으면 같은 신호를 두 번 세고 있다."""
    weights = {r.condition_type: r.weight for r in rules}
    fired: Dict[str, set] = {rule: set() for rule in TRANSACTION_SCOPED_RULES}
    for index, sample in enumerate(samples):
        _, triggered, _ = evaluate_signals(sample.signals, rules)
        for rule_type in triggered:
            fired[rule_type].add(index)

    rows = []
    for rule_a, rule_b in combinations(TRANSACTION_SCOPED_RULES, 2):
        set_a, set_b = fired[rule_a], fired[rule_b]
        if not set_a or not set_b:
            continue
        both = len(set_a & set_b)
        rows.append({
            "rule_a": rule_a,
            "rule_b": rule_b,
            "n_a": len(set_a),
            "n_b": len(set_b),
            "n_both": both,
            "jaccard": round(both / len(set_a | set_b), 3),
            "p_b_given_a": round(both / len(set_a), 3),
            "p_a_given_b": round(both / len(set_b), 3),
            "combined_points": round(weights.get(rule_a, 0) + weights.get(rule_b, 0), 1),
        })
    return sorted(rows, key=lambda row: -row["jaccard"])


def write_table(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="룰별 예측력과 임계값 근거")
    add_source_arguments(parser)
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    rules = default_rules()

    print("=" * 78)
    print(f"룰별 예측력  {describe(samples)}")
    print("=" * 78)

    stats = rule_stats(samples, rules)
    print(f"\n[1] 현재 임계값에서의 룰별 성능")
    print(f"  {'룰':<16}{'가중치':>7}{'임계값':>14}{'탐지수':>10}{'정밀도':>10}{'재현율':>9}{'lift':>8}")
    for row in stats:
        print(f"  {row['rule_type']:<16}{row['hand_weight']:>7.0f}{row['threshold']:>14,.2f}"
              f"{row['support']:>10,}{row['precision']:>10.4f}{row['recall']:>9.3f}{row['lift']:>8.2f}")

    measurable = [row for row in stats if row["support"] > 0]
    if len(measurable) >= 2:
        print(f"\n[2] 수기 가중치 순위 vs 실제 lift 순위")
        by_weight = [r["rule_type"] for r in sorted(measurable, key=lambda r: -r["hand_weight"])]
        by_lift = [r["rule_type"] for r in measurable]
        mismatches = sum(1 for a, b in zip(by_weight, by_lift) if a != b)
        for rank, (weight_rule, lift_rule) in enumerate(zip(by_weight, by_lift), 1):
            mark = "" if weight_rule == lift_rule else "   <- 불일치"
            print(f"  {rank}. 가중치 {weight_rule:<16} lift {lift_rule:<16}{mark}")
        print(f"  → {mismatches}개 순위에서 수기 가중치와 실제 예측력이 어긋난다.")

    curves: List[Dict[str, Any]] = []
    print(f"\n[3] 임계값 곡선 — 현재 값(*)이 곡선 어디에 있는가")
    for rule_type in TRANSACTION_SCOPED_RULES:
        rows = threshold_curve(samples, rule_type)
        if not rows:
            print(f"  {rule_type}: 이 데이터에는 해당 신호가 없다 — 건너뛴다")
            continue
        curves.extend(rows)
        print(f"\n  [{rule_type}]")
        print(f"    {'임계값':>16}{'탐지수':>10}{'탐지율':>9}{'정밀도':>10}{'재현율':>9}{'lift':>8}")
        for row in rows:
            mark = " *" if row["is_current"] else "  "
            print(f"  {mark}{row['threshold']:>16,.2f}{row['support']:>10,}"
                  f"{row['support_rate']:>9.3f}{row['precision']:>10.4f}"
                  f"{row['recall']:>9.3f}{row['lift']:>8.2f}")
        best = max(rows, key=lambda r: r["lift"])
        current = next((r for r in rows if r["is_current"]), None)
        if current and best["threshold"] != current["threshold"]:
            print(f"    → lift 최대는 {best['threshold']:,.2f} (lift {best['lift']:.2f}), "
                  f"현재 {current['threshold']:,.2f} (lift {current['lift']:.2f})")
            print(f"      단, lift가 높은 임계값은 탐지량이 적다. 어디를 고를지는")
            print(f"      탐지 누락 비용과 검토 공수의 비율이 정한다 (threshold.py).")

    overlaps = overlap_stats(samples, rules)
    if overlaps:
        print(f"\n[4] 룰 중복 — 자카드가 높을수록 같은 신호를 두 번 세고 있다")
        print(f"  {'룰 A':<16}{'룰 B':<16}{'자카드':>9}{'P(B|A)':>9}{'합산점수':>10}")
        for row in overlaps:
            print(f"  {row['rule_a']:<16}{row['rule_b']:<16}{row['jaccard']:>9.3f}"
                  f"{row['p_b_given_a']:>9.3f}{row['combined_points']:>10.1f}")

    write_table(OUT / "rule_lift.csv", stats)
    write_table(OUT / "rule_threshold_curve.csv", curves)
    write_table(OUT / "rule_overlap.csv", overlaps)
    print(f"\n저장: {OUT / 'rule_lift.csv'}")
    print(f"      {OUT / 'rule_threshold_curve.csv'}")
    print(f"      {OUT / 'rule_overlap.csv'}")


if __name__ == "__main__":
    main()
