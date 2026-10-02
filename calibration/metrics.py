"""분류 성능 지표.

sklearn을 쓰지 않는다. 이 패키지는 라벨과 점수만 있으면 돌아가야 하고,
지표 정의가 코드에 드러나 있어야 임계값 논의를 할 때 무엇을 재고 있는지 다툴
여지가 없기 때문이다.

AUC는 정의를 그대로 구현한다 — 무작위로 고른 사기 거래의 점수가 무작위로 고른
정상 거래의 점수보다 높을 확률. 동점은 0.5로 센다(Mann-Whitney U와 동일).
룰 기반 점수는 값이 몇 개 안 되어 동점이 대량으로 발생하므로, 동점 처리를
얼버무리면 AUC가 실제보다 좋게 나온다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def specificity(self) -> float:
        return self.tn / (self.tn + self.fp) if (self.tn + self.fp) else 0.0

    @property
    def fpr(self) -> float:
        return self.fp / (self.fp + self.tn) if (self.fp + self.tn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        n = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / n if n else 0.0


def confusion_at(scores: Sequence[float], labels: Sequence[int], threshold: float) -> Confusion:
    """점수 >= threshold 를 양성(이상거래 의심)으로 판정했을 때의 혼동행렬."""
    tp = fp = fn = tn = 0
    for score, label in zip(scores, labels):
        predicted = score >= threshold
        if label and predicted:
            tp += 1
        elif not label and predicted:
            fp += 1
        elif label and not predicted:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, fn, tn)


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """AUC = P(사기 점수 > 정상 점수) + 0.5 · P(동점). 순위합으로 계산한다."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return float("nan")

    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average_rank = (i + j) / 2 + 1     # 1부터 시작하는 순위의 평균
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1

    rank_sum = sum(rank for rank, label in zip(ranks, labels) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def lift(precision: float, base_rate: float) -> float:
    """lift = P(사기 | 탐지) / P(사기). 1.0이면 아무 정보도 없는 룰이다."""
    return precision / base_rate if base_rate else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """q분위수(0~100). numpy 없이 선형 보간으로 계산한다."""
    if not values:
        return float("nan")
    ordered: List[float] = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution_summary(values: Sequence[float]) -> Tuple[float, float, float, float, float]:
    """(p25, median, p75, p95, p99) — 임계값 후보를 눈으로 고를 때 쓴다."""
    return (
        percentile(values, 25),
        percentile(values, 50),
        percentile(values, 75),
        percentile(values, 95),
        percentile(values, 99),
    )


def _groups_by_score_desc(scores: Sequence[float], labels: Sequence[int]) -> List[Tuple[float, int, int]]:
    """점수 내림차순으로 (점수, 건수, 사기 건수) 묶음. 동점은 한 묶음이다."""
    groups: dict = {}
    for score, label in zip(scores, labels):
        n, bad = groups.get(score, (0, 0))
        groups[score] = (n + 1, bad + (1 if label else 0))
    return [(score, n, bad) for score, (n, bad) in sorted(groups.items(), reverse=True)]


def average_precision(scores: Sequence[float], labels: Sequence[int]) -> float:
    """PR-AUC(평균 정밀도). 사기율이 0.3%인 데이터에서는 ROC-AUC보다 이 값이 정직하다 —
    정상 거래가 압도적이면 FPR이 조금만 움직여도 거짓경보 건수는 크게 늘기 때문이다.

    동점 묶음은 한 임계값으로 본다 (묶음 안의 순서를 임의로 정하지 않는다).
    """
    positives = sum(1 for label in labels if label)
    if positives == 0:
        return float("nan")
    ap = tp = flagged = 0
    for _, n, bad in _groups_by_score_desc(scores, labels):
        tp += bad
        flagged += n
        ap += (bad / positives) * (tp / flagged)
    return ap


def recall_at_budget(scores: Sequence[float], labels: Sequence[int], budget: float) -> float:
    """점수 상위 budget 비율(예: 0.005 = 0.5%)만 알림으로 보낼 때의 재현율.

    검토 인력이 정해져 있으면 "임계값 몇 점"이 아니라 "하루 몇 건"이 제약이다. 두 탐지기를
    같은 알림 건수에서 비교해야 공정하다 — 한쪽만 임계값을 올리면 FPR은 얼마든지 내려간다.

    예산 경계에 동점 묶음이 걸리면, 그 묶음에서 무작위로 골랐을 때의 기댓값으로 센다.
    점수 값이 몇 개뿐인 룰 점수에서는 이 처리가 없으면 정렬 순서가 결과를 정한다.
    """
    positives = sum(1 for label in labels if label)
    if positives == 0:
        return float("nan")
    remaining = budget * len(labels)
    caught = 0.0
    for _, n, bad in _groups_by_score_desc(scores, labels):
        if remaining <= 0:
            break
        take = min(n, remaining)
        caught += bad * (take / n)
        remaining -= take
    return caught / positives
