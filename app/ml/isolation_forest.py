"""Isolation Forest 모델 래퍼."""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

MODEL_PATH = Path(__file__).parent / "artifacts" / "if_model.joblib"


class IFModel:
    """학습·추론·저장을 캡슐화한 Isolation Forest 래퍼.

    anomaly_score()는 0~1 범위를 반환한다 (1 = 가장 이상).
    학습 데이터의 score_samples 분포를 min/max로 선형 정규화하므로
    같은 데이터셋 내에서 상대적 이상 정도를 직관적으로 비교할 수 있다.
    """

    def __init__(self) -> None:
        self.model: IsolationForest | None = None
        self.score_min: float = -0.5
        self.score_max: float = 0.0
        self.feature_mean: np.ndarray | None = None
        self.feature_std: np.ndarray | None = None

    def fit(self, X: np.ndarray, contamination: float = 0.013) -> None:
        self.feature_mean = X.mean(axis=0)
        self.feature_std = X.std(axis=0) + 1e-8
        self.model = IsolationForest(
            n_estimators=200,
            contamination=contamination,
            random_state=42,
            n_jobs=-1,
        )
        self.model.fit(X)
        # 학습셋 분포를 기억해 추론 시 정규화에 활용
        scores = self.model.score_samples(X)
        self.score_min = float(scores.min())
        self.score_max = float(scores.max())

    def anomaly_score(self, x: np.ndarray) -> float:
        """0~1 이상 점수. 1에 가까울수록 이상거래."""
        if self.model is None:
            return 0.0
        raw = float(self.model.score_samples(x.reshape(1, -1))[0])
        # score_min(가장 이상) → 1, score_max(가장 정상) → 0 으로 선형 매핑
        span = self.score_max - self.score_min
        if span < 1e-8:
            return 0.0
        normalized = (self.score_max - raw) / span
        return round(max(0.0, min(1.0, normalized)), 4)

    def z_scores(self, x: np.ndarray) -> np.ndarray:
        """피처별 z-score — 설명가능성에 활용."""
        if self.feature_mean is None:
            return np.zeros_like(x)
        return (x - self.feature_mean) / self.feature_std

    def save(self) -> None:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, MODEL_PATH)
        print(f"모델 저장: {MODEL_PATH}")

    @classmethod
    def load(cls) -> IFModel | None:
        if not MODEL_PATH.exists():
            return None
        return joblib.load(MODEL_PATH)
