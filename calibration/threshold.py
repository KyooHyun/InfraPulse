"""등급 경계 결정 — 40점·70점에 근거를 붙이는 자리.

현행 경계(MEDIUM 40, HIGH 70)는 "적당히 나눈 구간"이라는 것 외에 근거가 없다.
등급은 사람이 보고 행동하는 단위이므로(MEDIUM=검토 대기, HIGH=STR 자동 생성),
경계는 두 가지를 만족해야 한다.

  1) 단조성 — 등급이 올라갈수록 실제 사기율이 높아져야 한다.
     그렇지 않으면 등급이 정보를 담고 있지 않다.
  2) 비용 정합성 — 경보를 울릴 지점은 "F1이 최대인 곳"이 아니라
     "기대 손실이 최소인 곳"이다. 사기를 놓치는 비용과 정상 거래를 붙잡는 비용은
     같지 않기 때문이다.

핵심은 (2)다. F1은 FN과 FP를 같은 무게로 취급한다. 실무에서 그 둘의 무게는 전혀
다르고, 어느 쪽이 얼마나 무거운지는 분석이 아니라 조직이 정한다 — 이체 한 건을
놓쳤을 때의 손실과 검토 인력 1건당 공수가 다른 부서의 숫자이기 때문이다.
그래서 이 스크립트는 임계값 하나를 정답으로 내놓지 않고, 비용비에 따라 임계값이
어떻게 움직이는지를 표로 낸다.

먼저 calibration/reachability.py 를 돌려라. 데이터로 경계를 고르기 전에,
그 경계에 도달할 수 있는지부터 확인해야 한다.

실행:
    python -m calibration.threshold --source db
    python -m calibration.threshold --source paysim --csv evaluation/data/PS_...log.csv --limit 300000

산출: calibration/outputs/threshold_sweep.csv
      calibration/outputs/threshold_cost_sensitivity.csv
      calibration/outputs/level_fraud_rate.csv
      calibration/outputs/level_boundaries_recommended.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fds_engine import RISK_LEVEL_HIGH, RISK_LEVEL_MEDIUM, evaluate_signals, risk_level
from calibration.console import use_utf8_console
from calibration.dataset import add_source_arguments, describe, load_samples
from calibration.metrics import confusion_at, roc_auc
from calibration.rules import default_rules

OUT = Path(__file__).resolve().parent / "outputs"

# ── 가정 ─────────────────────────────────────────────────────────────────────
# 표본의 사기율을 모집단 사기율로 그대로 쓰면 기대비용이 크게 왜곡된다. PaySim은
# 사기 유형만 골라 담아 사기율이 0.1%대이고, Credit Card는 0.17%다. 실제 카드
# 이상거래 비율은 건수 기준 0.1% 안팎으로 알려져 있어 그 근처로 둔다.
# 얇은 가정이므로 아래에서 비용비를 흔들어 결론이 얼마나 버티는지 함께 낸다.
POPULATION_FRAUD_RATE = 0.001
POPULATION_SIZE = 100_000

# 사기를 놓치는 비용 ÷ 정상 거래를 붙잡는 비용.
# 기본값은 예시다 — 이 값을 정하는 것이 조직의 의사결정이고, 분석이 할 일은
# 값에 따라 결론이 어떻게 달라지는지 보여주는 것이다.
DEFAULT_COST_RATIO = 50
COST_RATIOS = [1, 2, 5, 10, 20, 50, 100, 200]

# 구간이 너무 작으면 그 등급의 사기율이 표본 몇 건에 휘둘린다.
MIN_BUCKET = 30


def score_samples(samples, rules) -> Tuple[List[float], List[int]]:
    scores, labels = [], []
    for sample in samples:
        score, _, _ = evaluate_signals(sample.signals, rules)
        scores.append(float(score))
        labels.append(sample.label)
    return scores, labels


def expected_cost(
    fnr: float,
    fpr: float,
    cost_ratio: float,
    prior: float = POPULATION_FRAUD_RATE,
    n: int = POPULATION_SIZE,
) -> float:
    """모집단 n건 기준 기대비용 (정상 거래 1건 오탐 = 1단위)."""
    missed = n * prior * fnr
    false_alarms = n * (1 - prior) * fpr
    return cost_ratio * missed + false_alarms


def sweep(scores: Sequence[float], labels: Sequence[int]) -> List[Dict[str, Any]]:
    rows = []
    for threshold in range(0, 101):
        c = confusion_at(scores, labels, threshold)
        rows.append({
            "threshold": threshold,
            "tp": c.tp, "fp": c.fp, "fn": c.fn, "tn": c.tn,
            "precision": round(c.precision, 6),
            "recall": round(c.recall, 4),
            "f1": round(c.f1, 4),
            "fpr": round(c.fpr, 4),
            "expected_cost": round(expected_cost(1 - c.recall, c.fpr, DEFAULT_COST_RATIO), 1),
        })
    return rows


def level_fraud_rates(scores: Sequence[float], labels: Sequence[int]) -> List[Dict[str, Any]]:
    """현행 경계(40/70)에서 등급별 실제 사기율."""
    buckets: Dict[str, List[int]] = {}
    for score, label in zip(scores, labels):
        buckets.setdefault(risk_level(score), []).append(label)

    rows = []
    for level in ("LOW", "MEDIUM", "HIGH"):
        members = buckets.get(level, [])
        rows.append({
            "level": level,
            "n": len(members),
            "n_fraud": sum(members),
            "fraud_rate": round(sum(members) / len(members), 6) if members else None,
        })
    return rows


def _information_value(buckets: List[Tuple[int, int]]) -> float:
    """구간별 (전체수, 사기수) → Information Value.

    신용평가에서 구간을 나눌 때 쓰는 기준이다. 각 구간이 사기/정상을 얼마나 다르게
    담고 있는지를 합산한 값으로, 클수록 구간 나누기가 정보를 많이 담는다.
    표본이 0인 칸에서 로그가 발산하지 않도록 0.5를 더해 보정한다(Haldane 보정).
    """
    total_bad = sum(bad for _, bad in buckets)
    total_good = sum(n - bad for n, bad in buckets)
    if not total_bad or not total_good:
        return 0.0

    iv = 0.0
    for n, bad in buckets:
        bad_share = (bad + 0.5) / (total_bad + 0.5 * len(buckets))
        good_share = (n - bad + 0.5) / (total_good + 0.5 * len(buckets))
        iv += (good_share - bad_share) * math.log(good_share / bad_share)
    return iv


def recommend_boundaries(
    scores: Sequence[float],
    labels: Sequence[int],
    n_levels: int = 3,
    min_bucket: int = MIN_BUCKET,
) -> Dict[str, Any]:
    """관측 사기율로 등급 경계를 다시 고른다.

    가능한 경계 조합을 전수 탐색하되 두 제약을 건다.
      · 구간별 사기율이 **단조 증가**해야 한다 — 등급의 정의 그 자체다.
      · 각 구간에 최소 표본이 있어야 한다 — 사기율이 몇 건에 휘둘리면 안 된다.
    그중 Information Value가 가장 큰 조합을 고른다.

    구간은 왼쪽 닫힘·오른쪽 열림 [lo, hi) 이다. risk_level()이 `score >= boundary`로
    판정하기 때문이다. 양쪽을 연 구간으로 짜면 경계값에 정확히 걸린 거래가 어느
    구간에도 들어가지 않은 채로 단조성을 판정하게 된다 — 아래 합계 검증이 그래서 있다.
    """
    paired = sorted(zip((int(s) for s in scores), labels))
    unique = sorted({score for score, _ in paired})
    if len(unique) < n_levels:
        return {}

    def buckets_for(cuts: Tuple[int, ...]) -> Optional[List[Tuple[int, int]]]:
        edges = [0, *cuts, 101]
        out = []
        for lo, hi in zip(edges, edges[1:]):
            members = [label for score, label in paired if lo <= score < hi]
            if len(members) < min_bucket:
                return None
            out.append((len(members), sum(members)))
        assert sum(n for n, _ in out) == len(paired), "구간 합이 표본 수와 다르다"
        return out

    best = None
    for cuts in combinations(unique[1:], n_levels - 1):
        buckets = buckets_for(cuts)
        if buckets is None:
            continue
        rates = [bad / n for n, bad in buckets]
        if any(b < a for a, b in zip(rates, rates[1:])):    # 단조 증가 위반
            continue
        iv = _information_value(buckets)
        if best is None or iv > best["iv"]:
            best = {"cuts": cuts, "buckets": buckets, "rates": rates, "iv": iv}

    if best is None:
        return {}

    best["boundaries"] = list(zip((0, *best["cuts"]), ("LOW", "MEDIUM", "HIGH")))
    return best


def optimal_threshold(
    scores: Sequence[float], labels: Sequence[int], cost_ratio: float
) -> Tuple[int, float]:
    """주어진 비용비에서 기대비용이 최소인 임계값과 그때의 기대비용."""
    best_threshold, best_cost = 0, float("inf")
    for threshold in range(0, 101):
        c = confusion_at(scores, labels, threshold)
        cost = expected_cost(1 - c.recall, c.fpr, cost_ratio)
        if cost < best_cost:
            best_cost, best_threshold = cost, threshold
    return best_threshold, best_cost


def cost_sensitivity(scores: Sequence[float], labels: Sequence[int]) -> List[Dict[str, Any]]:
    rows = []
    for ratio in COST_RATIOS:
        threshold, cost = optimal_threshold(scores, labels, ratio)
        c = confusion_at(scores, labels, threshold)
        rows.append({
            "cost_ratio_fn_over_fp": ratio,
            "optimal_threshold": threshold,
            "expected_cost": round(cost, 1),
            "precision": round(c.precision, 6),
            "recall": round(c.recall, 4),
            "implied_level": risk_level(threshold),
        })
    return rows


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
    parser = argparse.ArgumentParser(description="등급 경계 캘리브레이션")
    add_source_arguments(parser)
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    scores, labels = score_samples(samples, default_rules())
    auc = roc_auc(scores, labels)

    print("=" * 78)
    print(f"등급 경계 캘리브레이션  {describe(samples)}")
    print(f"룰 점수 AUC {auc:.3f}")
    print("=" * 78)

    print(f"\n[1] 현행 경계({RISK_LEVEL_MEDIUM:.0f}/{RISK_LEVEL_HIGH:.0f})에서 등급별 실제 사기율")
    levels = level_fraud_rates(scores, labels)
    print(f"  {'등급':<10}{'거래수':>12}{'사기':>8}{'사기율':>12}")
    previous, monotonic = None, True
    for row in levels:
        rate = row["fraud_rate"]
        text = "-" if rate is None else f"{rate:.4%}"
        print(f"  {row['level']:<10}{row['n']:>12,}{row['n_fraud']:>8,}{text:>12}")
        if rate is not None:
            if previous is not None and rate < previous:
                monotonic = False
            previous = rate
    print(f"  → 등급이 오를수록 사기율이 높아지는가: "
          f"{'예 (단조 증가)' if monotonic else '아니오 — 등급 경계 재설정 필요'}")
    empty = [row["level"] for row in levels if row["n"] == 0]
    if empty:
        print(f"  → {', '.join(empty)} 등급에 해당하는 거래가 한 건도 없다 "
              f"(calibration/reachability.py 가 이유를 설명한다).")

    rows = sweep(scores, labels)
    best_f1 = max(rows, key=lambda r: r["f1"])
    print(f"\n[2] 임계값별 성능")
    print(f"  F1 최대: 임계값 {best_f1['threshold']}점 "
          f"(F1={best_f1['f1']:.4f}, 정밀도={best_f1['precision']:.4f}, "
          f"재현율={best_f1['recall']:.3f})")

    print(f"\n[3] 비용비에 따른 최적 임계값 "
          f"(모집단 사기율 {POPULATION_FRAUD_RATE:.2%} 가정, {POPULATION_SIZE:,}건 기준)")
    print(f"  {'FN비용/FP비용':>14}{'임계값':>8}{'정밀도':>10}{'재현율':>9}{'해당등급':>10}")
    sensitivity = cost_sensitivity(scores, labels)
    for row in sensitivity:
        print(f"  {row['cost_ratio_fn_over_fp']:>14}{row['optimal_threshold']:>8}"
              f"{row['precision']:>10.4f}{row['recall']:>9.3f}{row['implied_level']:>10}")
    spread = {row["optimal_threshold"] for row in sensitivity}
    print(f"  → 비용비 {COST_RATIOS[0]}~{COST_RATIOS[-1]} 구간에서 최적 임계값이 "
          f"{min(spread)}~{max(spread)}점으로 움직인다.")
    print(f"    임계값은 데이터만으로 정해지지 않는다. 비용비를 정하는 것이 조직의 결정이고,")
    print(f"    분석이 할 수 있는 건 그 결정이 결과를 어디까지 바꾸는지 보여주는 것이다.")

    print(f"\n[4] 관측 사기율로 등급 경계를 다시 고르면")
    best = recommend_boundaries(scores, labels)
    if not best:
        recommended = []
        print(f"  단조 증가 + 최소표본({MIN_BUCKET}건) 조건을 만족하는 경계 조합이 없다.")
        print(f"  룰 점수가 가질 수 있는 값이 몇 개뿐이라(reachability.py의 점수표)")
        print(f"  경계를 어디에 두든 같은 구간으로 묶이는 경우가 많다.")
    else:
        edges = [b for b, _ in best["boundaries"]] + [101]
        print(f"  {'등급':<10}{'점수 구간':>14}{'거래수':>12}{'사기':>8}{'사기율':>12}")
        for index, ((boundary, level), (n, bad)) in enumerate(zip(best["boundaries"], best["buckets"])):
            lo, hi = edges[index], edges[index + 1] - 1
            span = f"{lo}–{hi}" if hi < 100 else f"{lo}–100"
            print(f"  {level:<10}{span:>14}{n:>12,}{bad:>8,}{bad / n:>12.4%}")
        print(f"  Information Value {best['iv']:.4f} — 단조 증가 조건 충족")
        print(f"  현행 경계 [0, {RISK_LEVEL_MEDIUM:.0f}, {RISK_LEVEL_HIGH:.0f}] "
              f"→ 권고 {[b for b, _ in best['boundaries']]}")
        recommended = [
            {"level": level, "min_score": boundary, "n": n, "n_fraud": bad,
             "fraud_rate": round(bad / n, 6)}
            for (boundary, level), (n, bad) in zip(best["boundaries"], best["buckets"])
        ]

    write_table(OUT / "threshold_sweep.csv", rows)
    write_table(OUT / "threshold_cost_sensitivity.csv", sensitivity)
    write_table(OUT / "level_fraud_rate.csv", levels)
    write_table(OUT / "level_boundaries_recommended.csv", recommended)
    print(f"\n저장: {OUT / 'threshold_sweep.csv'}")
    print(f"      {OUT / 'threshold_cost_sensitivity.csv'}")
    print(f"      {OUT / 'level_fraud_rate.csv'}")
    print(f"      {OUT / 'level_boundaries_recommended.csv'}")


if __name__ == "__main__":
    main()
