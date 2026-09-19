"""점수 공간 도달 가능성 — 데이터 없이도 확인할 수 있는 임계값의 근거.

임계값을 정당화하는 일반적인 방법은 라벨 붙은 데이터로 lift와 비용을 재는 것이다
(rule_lift.py, threshold.py). 그런데 그 전에, **데이터가 한 건도 없어도** 확인할 수
있는 것이 하나 있다: 정해 놓은 임계값에 애초에 도달할 수 있는가.

위험점수는 트리거된 룰의 가중치 합이다. 그러므로 거래 한 건이 받을 수 있는 최대
점수는 "거래 단위로 판정되는 룰 전부가 동시에 발화했을 때의 가중치 합"으로 정해져
있다. 이 값이 등급 경계보다 작으면 그 등급은 **정의상 도달 불가능**하고, 그 경계에
걸린 모든 처리(여기서는 STR 자동 생성)는 실행되지 않는 코드가 된다.

이건 통계적 주장이 아니라 산수다. 표본 수도, 데이터셋도, 가정도 필요 없다.
그래서 캘리브레이션의 첫 단계로 둔다 — 데이터를 모으기 전에 끝내야 하는 점검이다.

실행: python -m calibration.reachability
산출: calibration/outputs/reachability.json
"""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fds_engine import (
    DEFAULT_RULES,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_MEDIUM,
    TRANSACTION_SCOPED_RULES,
)
from app.ml.ensemble import RULE_ALPHA
from calibration.console import use_utf8_console

OUT = Path(__file__).resolve().parent / "outputs"

LEVELS = (("MEDIUM", RISK_LEVEL_MEDIUM), ("HIGH", RISK_LEVEL_HIGH))


def rule_weights(rules: List[Dict[str, Any]] | None = None) -> Dict[str, float]:
    """룰 유형 → 가중치. 기본은 시드 룰이며, DB에서 읽은 룰을 넣어도 된다."""
    source = rules if rules is not None else DEFAULT_RULES
    return {r["condition_type"]: float(r["weight"]) for r in source}


def reachable_scores(weights: Dict[str, float]) -> List[Dict[str, Any]]:
    """거래 단위 룰의 모든 발화 조합과 그 점수."""
    scoped = [rule for rule in TRANSACTION_SCOPED_RULES if rule in weights]
    rows: List[Dict[str, Any]] = []
    for size in range(len(scoped) + 1):
        for fired in combinations(scoped, size):
            score = min(sum(weights[rule] for rule in fired), 100.0)
            rows.append({
                "fired_rules": list(fired),
                "n_fired": len(fired),
                "score": round(score, 1),
                "level": _level_of(score),
            })
    return sorted(rows, key=lambda row: -row["score"])


def _level_of(score: float) -> str:
    if score >= RISK_LEVEL_HIGH:
        return "HIGH"
    if score >= RISK_LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"


def orphan_rules(weights: Dict[str, float]) -> List[Dict[str, Any]]:
    """정의돼 있지만 거래 위험점수에 기여하지 않는 룰.

    LOGIN_FAILURE와 LATENCY는 각각 인증 경로와 미들웨어에서 처리되는 시스템 신호다.
    fds_rules 테이블에 가중치를 달고 앉아 있어서 "룰 5개 × 총 가중치 100점" 처럼
    보이지만, 거래 한 건이 받을 수 있는 점수에는 한 점도 보태지 않는다.
    등급 경계를 총 가중치 기준으로 잡았다면 그 순간부터 경계가 어긋난다.
    """
    return [
        {"rule_type": rule_type, "weight": weight}
        for rule_type, weight in weights.items()
        if rule_type not in TRANSACTION_SCOPED_RULES
    ]


def analyze(weights: Dict[str, float] | None = None) -> Dict[str, Any]:
    weights = weights or rule_weights()
    rows = reachable_scores(weights)
    max_rule_score = max(row["score"] for row in rows)

    orphans = orphan_rules(weights)
    declared_total = sum(weights.values())

    levels: List[Dict[str, Any]] = []
    for name, boundary in LEVELS:
        qualifying = [row for row in rows if row["score"] >= boundary]
        minimal = _minimal_combinations(qualifying)
        levels.append({
            "level": name,
            "boundary": boundary,
            "reachable_by_rules": bool(qualifying),
            "n_combinations": len(qualifying),
            "minimal_combinations": minimal,
            "shortfall": round(boundary - max_rule_score, 1) if not qualifying else 0.0,
        })

    # ML 앙상블이 붙으면 점수 상한이 달라진다.
    #   ensemble = α·(rule/100) + (1-α)·if_score,  if_score ∈ [0, 1]
    max_ensemble = round((RULE_ALPHA * (max_rule_score / 100.0) + (1 - RULE_ALPHA) * 1.0) * 100.0, 1)

    return {
        "rule_weights": weights,
        "transaction_scoped_rules": list(TRANSACTION_SCOPED_RULES),
        "declared_weight_total": round(declared_total, 1),
        "max_reachable_rule_score": max_rule_score,
        "orphan_rules": orphans,
        "orphan_weight_total": round(sum(o["weight"] for o in orphans), 1),
        "levels": levels,
        "ensemble": {
            "rule_alpha": RULE_ALPHA,
            "max_reachable_score": max_ensemble,
            "high_reachable": max_ensemble >= RISK_LEVEL_HIGH,
        },
        "reachable_score_table": rows,
    }


def _minimal_combinations(qualifying: List[Dict[str, Any]]) -> List[List[str]]:
    """해당 등급에 도달하는 조합 중 더 줄일 수 없는 것들.

    "그 등급이 뜨려면 최소한 무엇이 동시에 일어나야 하는가"가 임계값의 실질적 의미다.
    """
    combos = [set(row["fired_rules"]) for row in qualifying]
    minimal = [
        combo for combo in combos
        if not any(other < combo for other in combos)
    ]
    return sorted((sorted(combo) for combo in minimal), key=lambda c: (len(c), c))


def main() -> None:
    use_utf8_console()
    result = analyze()

    print("=" * 78)
    print("점수 공간 도달 가능성 — 데이터 없이 확인하는 임계값 정합성")
    print("=" * 78)

    print(f"\n[1] 룰 가중치")
    print(f"  {'룰':<16}{'가중치':>8}  {'거래 점수 기여':>14}")
    for rule_type, weight in result["rule_weights"].items():
        scoped = rule_type in result["transaction_scoped_rules"]
        print(f"  {rule_type:<16}{weight:>8.1f}  {'예' if scoped else '아니오 (시스템 신호)':>14}")
    print(f"  선언된 가중치 합계 {result['declared_weight_total']:.1f}점 중 "
          f"{result['orphan_weight_total']:.1f}점은 거래 점수에 기여하지 않는다.")

    print(f"\n[2] 거래 한 건이 받을 수 있는 점수")
    print(f"  {'발화 룰':<44}{'점수':>7}{'등급':>8}")
    for row in result["reachable_score_table"]:
        fired = ", ".join(row["fired_rules"]) or "(없음)"
        print(f"  {fired:<44}{row['score']:>7.1f}{row['level']:>8}")
    print(f"  → 최대 도달 점수 {result['max_reachable_rule_score']:.1f}점")

    print(f"\n[3] 등급 경계 도달 가능성")
    for level in result["levels"]:
        if level["reachable_by_rules"]:
            combos = " / ".join("+".join(c) for c in level["minimal_combinations"])
            print(f"  {level['level']:<8}{level['boundary']:>6.0f}점  도달 가능 — 최소 조합: {combos}")
        else:
            print(f"  {level['level']:<8}{level['boundary']:>6.0f}점  **도달 불가** — "
                  f"{level['shortfall']:.1f}점 모자란다")

    unreachable = [lv for lv in result["levels"] if not lv["reachable_by_rules"]]
    if unreachable:
        names = ", ".join(lv["level"] for lv in unreachable)
        print(f"\n  → 룰만으로는 {names} 등급에 도달할 수 없다. 이 경계에 걸린 처리는")
        print(f"    실행되지 않는 코드다. 임계값이 틀렸거나 가중치가 틀렸거나 둘 중 하나이고,")
        print(f"    어느 쪽인지는 산수가 아니라 데이터로 정해야 한다(threshold.py).")

    ensemble = result["ensemble"]
    print(f"\n[4] ML 앙상블을 켰을 때 (α={ensemble['rule_alpha']})")
    print(f"  최대 도달 점수 {ensemble['max_reachable_score']:.1f}점 — "
          f"HIGH 도달 {'가능' if ensemble['high_reachable'] else '불가'}")
    print(f"  즉 HIGH 등급은 ML이 켜진 구성에서만 의미가 있다. 룰 전용 구성에서는")
    print(f"  경계가 닿지 않으므로, 두 구성이 같은 임계값을 쓰는 것 자체가 근거 없는 설정이다.")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "reachability.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {path}")


if __name__ == "__main__":
    main()
