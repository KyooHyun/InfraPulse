"""Credit Card Fraud 오프라인 평가 파이프라인 — 룰 단독 vs 룰+IF 앙상블 비교.

PaySim 대신 ULB Credit Card Fraud 데이터셋을 사용하는 이유:
  PaySim의 고액 거래 집합 내 IF 분리도 진단:
    사기 p25=45.1 ≈ 정상 p75=46.9 (거의 완전 중첩)
  원인: 고액 거래는 사기·정상 무관하게 이상치로 잡혀 IF가 변별 불가.
  이 데이터셋은 V1-V28 PCA 피처를 통해 사기 패턴이 직접 인코딩되어
  IF가 고액 거래 집합 내에서도 명확히 분리 가능.

사용법:
    python evaluation/creditcard_eval.py --csv path/to/creditcard.csv

데이터셋: https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud
결과: evaluation/results.json (API /fds/comparison에서 반환)

비교 방법론:
  - 룰 기반: HIGH_VALUE (amount >= p95) → rule_score = 45 (> 임계값 40)
  - IF 모델: 비사기 거래로 학습, V1-V28 + log_amount 기반 이상 점수 [0, 100]
  - 앙상블: hybrid = 0.4 * rule_score + 0.6 * if_score
      • rule 없음 + if_score >= 66.7 → 룰 미탐지 사기 추가 포착 (Recall↑)
      • rule 있음 + if_score < 36.7 → 정상 고액 거래 해제 (FPR↓)
  - 판정 기준: 점수 >= 40 → 이상거래 의심 (라이브 시스템 MEDIUM 기준과 동일)
  - 정답 라벨: Class 컬럼 (0=정상, 1=사기)
"""

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import confusion_matrix

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RESULTS_PATH = Path(__file__).parent / "results.json"

FLAG_THRESHOLD = 40.0
RULE_SCORE_HIGH_VALUE = 45.0   # > 40 → 룰 단독으로 탐지
IF_ALPHA = 0.4                 # 룰 비중 40%, IF 비중 60%
# hybrid = 0.4*45 + 0.6*if = 18 + 0.6*if
# • un-flag 조건: if_score < 36.7 → 고액 정상 거래 해제
# • 룰 미탐지 포착: if_score >= 66.7 → 비고액 사기 포착


def _extract_if_features(df: pd.DataFrame) -> np.ndarray:
    """IF 학습/추론용 피처 행렬.

    V1-V28: PCA 변환된 사기 패턴 피처 (사기 패턴 직접 인코딩, 룰과 직교 신호)
    log_amount: 거래 금액 로그 (보조)
    룰 신호(Amount 임계값)와 IF 신호(V1-V28 패턴)가 서로 다른 차원 — 앙상블 효과 극대화
    """
    v_cols = [f"V{i}" for i in range(1, 29)]
    return np.hstack([
        df[v_cols].values,
        np.log1p(df["Amount"].values).reshape(-1, 1),
    ])


def _evaluate(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    y_pred = (scores >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "threshold": threshold,
        "true_positive_rate": round(recall, 4),
        "false_positive_rate": round(fpr, 4),
        "precision": round(precision, 4),
        "f1_score": round(f1, 4),
        "n_flagged": int(y_pred.sum()),
        "n_frauds_caught": int(tp),
        "n_false_alarms": int(fp),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Credit Card Fraud FDS 성능 비교 평가")
    parser.add_argument("--csv", required=True, help="creditcard.csv 경로")
    args = parser.parse_args()

    # ── 데이터 로드 ────────────────────────────────────────────────────────────
    logger.info("Credit Card Fraud CSV 로드: %s", args.csv)
    df = pd.read_csv(args.csv)
    n_fraud = int(df["Class"].sum())
    logger.info("로드 완료 — 전체: %d건, 사기: %d건 (%.3f%%)", len(df), n_fraud, n_fraud / len(df) * 100)

    # ── 룰 기반 점수: HIGH_VALUE ───────────────────────────────────────────────
    hv_threshold = float(df["Amount"].quantile(0.95))
    logger.info("HIGH_VALUE 임계값 (p95): %.2f", hv_threshold)
    rule_scores = np.where(df["Amount"].values >= hv_threshold, RULE_SCORE_HIGH_VALUE, 0.0)
    logger.info("룰 탐지 건수: %d건", int((rule_scores >= FLAG_THRESHOLD).sum()))

    # ── IF 모델 학습 (비사기 거래, V1-V28 + log_amount) ───────────────────────
    non_fraud_df = df[df["Class"] == 0].sample(min(50_000, (df["Class"] == 0).sum()), random_state=42)
    X_train = _extract_if_features(non_fraud_df)
    logger.info("IF 모델 학습 중 — 비사기 %d건 (V1-V28 + log_amount, 29 피처)...", len(non_fraud_df))
    clf = IsolationForest(n_estimators=200, contamination=0.01, random_state=42)
    clf.fit(X_train)

    # ── 전체 IF 이상 점수 계산 ─────────────────────────────────────────────────
    X_all = _extract_if_features(df)
    raw_if = clf.decision_function(X_all)
    if_scores = np.clip((-raw_if + 0.5) / 1.0 * 100.0, 0.0, 100.0)

    # ── 진단: 사기 vs 정상 IF 점수 분포 ──────────────────────────────────────
    fraud_mask = df["Class"].values == 1
    rule_flagged = rule_scores >= FLAG_THRESHOLD
    logger.info(
        "IF 점수 (전체)     — 사기: median=%.1f p25=%.1f p75=%.1f | 정상: median=%.1f p25=%.1f p75=%.1f",
        float(np.median(if_scores[fraud_mask])),
        float(np.percentile(if_scores[fraud_mask], 25)),
        float(np.percentile(if_scores[fraud_mask], 75)),
        float(np.median(if_scores[~fraud_mask])),
        float(np.percentile(if_scores[~fraud_mask], 25)),
        float(np.percentile(if_scores[~fraud_mask], 75)),
    )
    rule_fraud = rule_flagged & fraud_mask
    rule_normal = rule_flagged & ~fraud_mask
    if rule_fraud.sum() > 0 and rule_normal.sum() > 0:
        logger.info(
            "IF 점수 (룰 탐지)  — 사기 %d건: median=%.1f p25=%.1f | 정상 %d건: median=%.1f p75=%.1f",
            int(rule_fraud.sum()), float(np.median(if_scores[rule_fraud])),
            float(np.percentile(if_scores[rule_fraud], 25)),
            int(rule_normal.sum()), float(np.median(if_scores[rule_normal])),
            float(np.percentile(if_scores[rule_normal], 75)),
        )

    # ── 앙상블 점수 ───────────────────────────────────────────────────────────
    hybrid_scores = np.clip(IF_ALPHA * rule_scores + (1.0 - IF_ALPHA) * if_scores, 0.0, 100.0)

    # ── 성능 평가 ─────────────────────────────────────────────────────────────
    y_true = df["Class"].values
    rule_metrics = _evaluate(y_true, rule_scores, FLAG_THRESHOLD)
    hybrid_metrics = _evaluate(y_true, hybrid_scores, FLAG_THRESHOLD)

    fpr_rule = rule_metrics["false_positive_rate"]
    fpr_hybrid = hybrid_metrics["false_positive_rate"]
    fpr_reduction = (fpr_rule - fpr_hybrid) / fpr_rule * 100.0 if fpr_rule > 0 else 0.0
    recall_delta = (hybrid_metrics["true_positive_rate"] - rule_metrics["true_positive_rate"]) * 100.0

    results = {
        "note": "Credit Card Fraud(ULB) 평가 결과. PaySim 대비 개선 사유: V1-V28 PCA 피처로 IF 분리도 확보.",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": f"ULB Credit Card Fraud (n={len(df):,}, fraud={n_fraud:,})",
        "rule_description": f"HIGH_VALUE: Amount >= p95({hv_threshold:.2f}) → {RULE_SCORE_HIGH_VALUE}점",
        "flag_threshold": FLAG_THRESHOLD,
        "alpha_rule": IF_ALPHA,
        "alpha_if": round(1.0 - IF_ALPHA, 1),
        "rule_only": rule_metrics,
        "hybrid_rule_if": hybrid_metrics,
        "improvement": {
            "fpr_reduction_pct": round(fpr_reduction, 2),
            "recall_delta_pct": round(recall_delta, 2),
        },
        "paysim_note": (
            "PaySim으로 먼저 시도했으나 룰 탐지 집합 내 IF 분리도 불량 "
            "(사기 p25=45.1 ≈ 정상 p75=46.9)으로 개선 없었음. "
            "고액 거래가 사기·정상 무관하게 이상치로 분류되는 PaySim 구조적 한계."
        ),
    }

    RESULTS_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("결과 저장 완료 → %s", RESULTS_PATH)
    logger.info(
        "룰 단독   — FPR: %.2f%%, Recall: %.2f%%, F1: %.4f, 탐지: %d건",
        fpr_rule * 100, rule_metrics["true_positive_rate"] * 100,
        rule_metrics["f1_score"], rule_metrics["n_flagged"],
    )
    logger.info(
        "룰+IF 앙상블 — FPR: %.2f%%, Recall: %.2f%%, F1: %.4f, 탐지: %d건  ← FPR %+.1f%%p  Recall %+.1f%%p",
        fpr_hybrid * 100, hybrid_metrics["true_positive_rate"] * 100,
        hybrid_metrics["f1_score"], hybrid_metrics["n_flagged"],
        -fpr_reduction, recall_delta,
    )


if __name__ == "__main__":
    main()
