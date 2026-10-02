"""알림 임계값 — 점수 분포에서 "상위 budget 비율" 지점을 찾는다.

검토 인력이 정해져 있으면 제약은 "몇 점 이상"이 아니라 "하루 몇 건"이다. 그런데 학습 구간에서
상위 0.5%로 맞춘 고정 임계값이 평가 구간에서는 0.9~1.5%를 알림으로 보냈다 — 점수 분포가
시간에 따라 이동하기 때문이다(evaluation/README.md).

그래서 임계값을 최근 N일의 점수 분포로 주기적으로 다시 계산한다. 레이블이 필요 없으므로
운영에서 매일 돌릴 수 있다. 평가(scripts/evaluate.py)도 이 함수를 그대로 쓴다.
"""
from __future__ import annotations

from typing import Dict, List, Sequence


def budget_threshold(scores: Sequence[float], budget: float) -> float:
    """점수 상위 budget 비율이 되는 지점. 이 값 이상을 알림으로 보낸다.

    동점이 많으면(점수 값의 종류가 적으면) 실제 알림 비율은 budget보다 커진다.
    """
    if not scores:
        return float("inf")
    ordered = sorted(scores, reverse=True)
    return ordered[max(int(budget * len(ordered)) - 1, 0)]


def rolling_thresholds(
    scores_by_day: Dict[int, List[float]],
    days: Sequence[int],
    window_days: int,
    budget: float,
) -> Dict[int, float]:
    """각 날짜의 임계값 = 직전 window_days일 점수의 상위 budget 지점.

    당일 점수는 쓰지 않는다 — 하루가 끝나야 알 수 있는 분포로 그날의 알림을 정하면 미래를 보는 것이다.
    """
    out: Dict[int, float] = {}
    for day in days:
        recent: List[float] = []
        for past in range(day - window_days, day):
            recent.extend(scores_by_day.get(past, ()))
        out[day] = budget_threshold(recent, budget)
    return out
