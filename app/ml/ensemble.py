"""룰 점수 + Isolation Forest 점수 앙상블 및 설명가능성."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .features import FEATURE_NAMES

# 앙상블 가중치 (α: 룰 비중, 1-α: ML 비중)
# evaluate.py에서 최적 α를 찾고 여기 반영할 수 있다.
RULE_ALPHA = 0.5


def ensemble_score(rule_score: float, if_score: float, alpha: float = RULE_ALPHA) -> float:
    """룰 점수(0~100)와 IF 이상 점수(0~1)를 결합해 앙상블 점수(0~100)를 반환한다."""
    combined = alpha * (rule_score / 100.0) + (1.0 - alpha) * if_score
    return round(min(combined * 100.0, 100.0), 2)


def build_explanation(
    rule_score: float,
    contributions: List[Dict[str, Any]],
    if_score: Optional[float],
    feature_vector: Optional[np.ndarray],
    model: Any,  # IFModel — 순환 임포트 방지를 위해 Any
) -> Dict[str, Any]:
    """알림 1건에 대한 탐지 근거 딕셔너리를 반환한다.

    rule_contributions: 각 룰의 트리거 여부·기여 가중치·임계값·실제값
    ml_top_anomalous_features: z-score 기준 상위 3개 이상 피처 (모델 미학습 시 빈 리스트)
    """
    rule_contributions = [
        {
            "type": c["rule_type"],
            "fired": c["fired"],
            "weight": c["weight"],
            "threshold": c["threshold"],
            "actual_value": c.get("actual_value"),
        }
        for c in contributions
    ]

    ml_top: List[Dict[str, Any]] = []
    if if_score is not None and feature_vector is not None and model is not None:
        zs = model.z_scores(feature_vector)
        top3 = sorted(enumerate(zs), key=lambda t: abs(t[1]), reverse=True)[:3]
        ml_top = [
            {
                "feature": FEATURE_NAMES[i],
                "value": round(float(feature_vector[i]), 4),
                "z_score": round(float(zs[i]), 2),
            }
            for i, _ in top3
        ]

    return {
        "rule_score": round(rule_score, 1),
        "rule_contributions": rule_contributions,
        "ml_anomaly_score": round(if_score * 100, 1) if if_score is not None else None,
        "ml_top_anomalous_features": ml_top,
    }
