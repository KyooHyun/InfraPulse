#!/usr/bin/env python
"""Isolation Forest 학습 및 모델 아티팩트 저장.

PaySim 공개 데이터셋을 외부 벤치마크로 사용하며, 사기 거래를 자체 생성하지 않습니다.
레이블은 학습이 아니라 성능 평가와 모델 튜닝 검증용으로만 활용됩니다.

Usage:
    python scripts/train_model.py

load_paysim.py 실행 후 DB에 is_fraud 레이블이 있는 거래가 있어야 한다.
학습은 비지도(unsupervised)로 수행하며, 레이블은 성능 평가에만 사용된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.db import SessionLocal
from app import models
from app.ml.features import extract_features, FEATURE_NAMES
from app.ml.isolation_forest import IFModel


def main() -> None:
    db = SessionLocal()
    try:
        print("레이블된 거래 로딩 중...")
        txs = (
            db.query(models.Transaction)
            .filter(models.Transaction.is_fraud.isnot(None))
            .all()
        )
        if not txs:
            print("레이블된 거래가 없습니다. scripts/load_paysim.py를 먼저 실행하세요.")
            return

        n_fraud = sum(1 for tx in txs if tx.is_fraud)
        contamination = max(0.001, min(0.5, n_fraud / len(txs)))
        print(f"  총 {len(txs):,}건 | 사기 {n_fraud}건 ({contamination:.3%}) → contamination={contamination:.4f}")

        print("피처 추출 중 (velocity=0으로 배치 처리)...")
        # PaySim 계좌는 고유 ID라 velocity 집계가 의미 없으므로 0으로 고정
        X = np.array([extract_features(tx, db, velocity=0) for tx in txs])
        print(f"  피처 행렬: {X.shape}  |  피처: {FEATURE_NAMES}")

        print("Isolation Forest 학습 중...")
        model = IFModel()
        model.fit(X, contamination=contamination)
        model.save()

        # 학습셋 이상 점수 분포 요약
        fraud_idx = [i for i, tx in enumerate(txs) if tx.is_fraud]
        normal_idx = [i for i, tx in enumerate(txs) if not tx.is_fraud]
        fraud_scores = [model.anomaly_score(X[i]) for i in fraud_idx]
        normal_scores = [model.anomaly_score(X[i]) for i in normal_idx]

        print("\n── 학습셋 이상 점수 분포 ──────────────────────")
        print(f"  사기   | 평균 {np.mean(fraud_scores):.3f}  중앙값 {np.median(fraud_scores):.3f}  max {np.max(fraud_scores):.3f}")
        print(f"  정상   | 평균 {np.mean(normal_scores):.3f}  중앙값 {np.median(normal_scores):.3f}  max {np.max(normal_scores):.3f}")
        print("\n다음 단계: python scripts/evaluate.py")

    finally:
        db.close()


if __name__ == "__main__":
    main()
