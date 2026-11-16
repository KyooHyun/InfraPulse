"""보류(임시조치)의 오탐 비용 — HIGH로 보류되는 거래 중 정상 거래가 얼마나 되는가.

    python -m calibration.hold_cost --source paysim --csv evaluation/data/PS_20174392719_1491204439457_log.csv

HIGH(70점↑) 이체는 이제 실행되지 않고 보류된다(app/holds.py). 전에는 오탐이 담당자의 검토 시간만 썼지만,
이제는 정상 고객의 돈을 묶는다. 그래서 "보류된 거래 100건 중 몇 건이 정상인가"를 구간별로, 그리고 어떤
룰 조합으로 HIGH가 됐는지별로 센다. 점수는 운영과 같은 evaluate_signals·default_rules로 낸다.

구간은 scripts/evaluate.py의 시간 분할과 같다: 1~9일(룰 결정), 10~12일(검증), 13~17일(평가), 18~31일(참고).
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from app.fds_engine import RISK_LEVEL_HIGH, evaluate_signals
from calibration.console import use_utf8_console
from calibration.dataset import add_source_arguments, load_samples
from calibration.rules import default_rules

SEGMENTS = [("1~9일 (룰 결정)", 1, 9), ("10~12일 (검증)", 10, 12), ("13~17일 (평가)", 13, 17), ("18~31일 (참고)", 18, 31)]
OUTPUT = Path(__file__).resolve().parent / "outputs" / "hold_cost.csv"


def _day(step: int) -> int:
    return (step - 1) // 24 + 1


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_source_arguments(parser)
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    rules = default_rules()
    rows = []
    for name, first, last in SEGMENTS:
        seg = [s for s in samples if s.step is not None and first <= _day(s.step) <= last]
        if not seg:
            continue
        held, combos = [], Counter()
        for s in seg:
            score, triggered, _ = evaluate_signals(s.signals, rules)
            if score >= RISK_LEVEL_HIGH:
                held.append(s.label)
                combos[(" + ".join(sorted(triggered)), s.label)] += 1
        n, n_fraud, n_held, held_fraud = len(seg), sum(s.label for s in seg), len(held), sum(held)
        held_normal = n_held - held_fraud
        print(f"\n[{name}] 거래 {n:,}건, 사기 {n_fraud:,}건 ({n_fraud / n:.3%})")
        print(f"  보류(HIGH)       {n_held:,}건 = 거래의 {n_held / n:.3%}")
        if n_held:
            print(f"  그중 사기        {held_fraud:,}건 (정밀도 {held_fraud / n_held:.1%})")
            print(f"  그중 정상        {held_normal:,}건 → 보류 100건 중 정상 {100 * held_normal / n_held:.1f}건")
            print(f"  정상 거래 중 보류 {held_normal / (n - n_fraud):.3%}   사기 중 보류(재현율) {held_fraud / n_fraud:.3f}")
            print("  룰 조합별 (보류 건수 많은 순):")
            for combo in sorted({c for c, _ in combos}, key=lambda c: -(combos[(c, 0)] + combos[(c, 1)])):
                fraud, normal = combos[(combo, 1)], combos[(combo, 0)]
                print(f"    {combo:<60} {fraud + normal:>7,}건  정상 {normal:>7,}  사기 {fraud:>6,}"
                      f"  정밀도 {fraud / (fraud + normal):.1%}")
                rows.append({"segment": name, "rules": combo, "held": fraud + normal, "normal": normal, "fraud": fraud})
        rows.append({"segment": name, "rules": "(전체)", "held": n_held, "normal": held_normal, "fraud": held_fraud})

    OUTPUT.parent.mkdir(exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["segment", "rules", "held", "normal", "fraud"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n저장: {OUTPUT}")


if __name__ == "__main__":
    main()
