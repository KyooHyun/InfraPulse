#!/usr/bin/env python
"""룰 기반 vs Isolation Forest vs 앙상블 성능 비교표.

PaySim 공개 데이터셋을 기준으로, 룰 베이스라인과 ML 추가 도입 시
precision / recall / FPR 트레이드오프를 비교합니다.
PaySim 평가 시에는 데이터셋에 존재하는 룰 피처만 적용하며,
로그인 실패/거래 실패율/응답 지연 등 PaySim 원본에 직접 포함되지 않은 항목은 별도 평가 대상에서 제외됩니다.

Usage:
    python scripts/evaluate.py

출력 예시:
    방법                  임계값   Precision   Recall      FPR       F1
    룰 엔진               40       0.421       0.812       0.073     0.556
    룰 엔진               70       0.683       0.512       0.031     0.585
    Isolation Forest      0.40     0.619       0.731       0.029     0.671
    앙상블(α=0.5)         40       0.701       0.798       0.022     0.747

"룰만 쓸 때 vs 앙상블" 트레이드오프가 이 표의 핵심 스토리다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.db import SessionLocal
from app import models
from app.fds_engine import get_active_rules, evaluate_transaction
from app.ml.features import extract_features
from app.ml.isolation_forest import IFModel
from app.ml.ensemble import ensemble_score


def _metrics(y_true: list, y_pred: list) -> dict:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred) if not t and p)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t and not p)
    tn = sum(1 for t, p in zip(y_true, y_pred) if not t and not p)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return {"precision": prec, "recall": rec, "fpr": fpr, "f1": f1}


def _row(label: str, threshold: str, m: dict) -> str:
    return (
        f"{label:<22} {threshold:>8}  "
        f"{m['precision']:>9.3f}  {m['recall']:>8.3f}  "
        f"{m['fpr']:>8.3f}  {m['f1']:>8.3f}"
    )


def main() -> None:
    db = SessionLocal()
    ifm = IFModel.load()

    try:
        txs = (
            db.query(models.Transaction)
            .filter(models.Transaction.is_fraud.isnot(None))
            .all()
        )
        if not txs:
            print("레이블된 거래가 없습니다. load_paysim.py를 먼저 실행하세요.")
            return

        rules = get_active_rules(db)
        y_true = [bool(tx.is_fraud) for tx in txs]

        print(f"평가 중: {len(txs):,}건 (사기 {sum(y_true)}건, {sum(y_true)/len(txs)*100:.2f}%)...")

        rule_scores, if_scores, ens_scores = [], [], []
        for i, tx in enumerate(txs):
            if i % 10_000 == 0 and i > 0:
                print(f"  {i:,}/{len(txs):,}")
            r, _, _ = evaluate_transaction(tx.amount, tx.account_from, db, rules)
            rule_scores.append(r)

            if ifm is not None:
                feat = extract_features(tx, db, velocity=0)
                s = ifm.anomaly_score(feat)
                if_scores.append(s)
                ens_scores.append(ensemble_score(r, s))

        header = f"\n{'방법':<22} {'임계값':>8}  {'Precision':>9}  {'Recall':>8}  {'FPR':>8}  {'F1':>8}"
        sep = "─" * 72
        print(header)
        print(sep)

        for thresh in [40.0, 55.0, 70.0]:
            y_pred = [s >= thresh for s in rule_scores]
            print(_row("룰 엔진", f"{thresh:.0f}", _metrics(y_true, y_pred)))

        if ifm and if_scores:
            print(sep)
            for thresh in [0.35, 0.45, 0.55, 0.65]:
                y_pred = [s >= thresh for s in if_scores]
                print(_row("Isolation Forest", f"{thresh:.2f}", _metrics(y_true, y_pred)))

            print(sep)
            for thresh in [40.0, 55.0, 70.0]:
                y_pred = [s >= thresh for s in ens_scores]
                print(_row("앙상블(α=0.5)", f"{thresh:.0f}", _metrics(y_true, y_pred)))
        else:
            print("\nIsolation Forest 모델 미학습 — rule 베이스라인만 출력")
            print("  → python scripts/train_model.py 실행 후 재시도")

        print(sep)
        print(f"\n* 같은 Recall 기준에서 앙상블의 FPR이 낮을수록 ML 레이어의 기여가 크다.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
