#!/usr/bin/env python
"""Isolation Forest 학습 및 모델 아티팩트 저장.

PaySim 공개 데이터셋을 외부 벤치마크로 사용하며, 사기 거래를 자체 생성하지 않습니다.
학습은 비지도로 수행합니다. 레이블은 contamination(이상 비율) 추정과 평가에만 씁니다.

Usage:
    python scripts/train_model.py --source paysim --csv evaluation/data/PS_20174392719_1491204439457_log.csv
    python scripts/train_model.py --source db      # load_paysim.py로 적재한 거래

피처는 운영과 같은 app.ml.features.feature_values로 만든다(calibration.dataset.ml_feature_matrix).
예전에는 velocity를 0으로 고정해 학습했고 운영은 실제 velocity를 넣었다 — 모델이 학습 때
본 적 없는 값을 운영에서 받는 학습-서빙 불일치였다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.ml.features import FEATURE_NAMES
from app.ml.isolation_forest import IFModel
from calibration.console import use_utf8_console
from calibration.dataset import add_source_arguments, describe, load_samples, ml_feature_matrix


def main() -> None:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="Isolation Forest 학습")
    add_source_arguments(parser)
    args = parser.parse_args()

    samples = load_samples(args.source, args.csv, args.limit)
    print(describe(samples))
    labels = np.array([s.label for s in samples])
    contamination = float(max(0.001, min(0.5, labels.mean())))

    X = ml_feature_matrix(samples)
    print(f"피처 행렬: {X.shape}  |  피처: {FEATURE_NAMES}  |  contamination={contamination:.4f}")

    model = IFModel()
    model.fit(X, contamination=contamination)
    model.save()

    scores = model.anomaly_scores(X)
    print("\n── 학습셋 이상 점수 분포 ──────────────────────")
    for name, mask in (("사기", labels == 1), ("정상", labels == 0)):
        part = scores[mask]
        print(f"  {name}   | 평균 {part.mean():.3f}  중앙값 {np.median(part):.3f}  max {part.max():.3f}")
    print("\n다음 단계: python scripts/evaluate.py (같은 --source/--csv)")


if __name__ == "__main__":
    main()
