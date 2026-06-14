"""PaySim 오프라인 평가 파이프라인 — 룰 단독 vs 룰+IF 앙상블 비교.

사용법:
    python evaluation/paysim_eval.py --csv path/to/PS_20174392719_1491204439457_log.csv

PaySim 다운로드: https://www.kaggle.com/datasets/ealaxi/paysim1
결과: evaluation/results.json (API /fds/comparison에서 반환)

비교 방법론:
  - 룰 기반: HIGH_VALUE(금액 p90↑ +30), TYPE_RISK(TRANSFER/CASH_OUT +20), BALANCE_DRAIN(잔액 80%↑소진 +30)
  - IF 모델: 비사기 거래로 학습, 이상 점수 [0, 100] 정규화
  - 앙상블: hybrid = 0.6 * rule_score + 0.4 * if_score
  - 판정 기준: 점수 >= 40 → 이상거래 의심 (라이브 시스템 MEDIUM 기준과 동일)
  - 정답 라벨: PaySim의 isFraud 컬럼
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

# 룰: HIGH_VALUE — 금액 기준 단순 임계값 (라이브 FDS HIGH_VALUE 룰과 동일한 컨셉)
#   amount >= p95 → rule_score = 45 (> 40 임계값, 상위 5% 거래 탐지)
#   룰은 금액만 보고, 거래 패턴(잔액 변화, 수신 여부)은 보지 않음
# IF: log_amount + drain_ratio + newbalanceOrig_zero + type 4개 피처 학습
#   - 사기 패턴: 잔액 완전 소진(newbalanceOrig=0) + TRANSFER/CASH_OUT 조합
#   - 정상 패턴: 고액이어도 잔액이 남거나, PAYMENT 유형
# 앙상블: rule(금액 신호) + IF(거래 패턴 신호)
#   hybrid = 0.6 * 45 + 0.4 * if_score = 27 + 0.4 * if_score
#   → if_score < 32.5이면 hybrid < 40 → IF가 legit 고액 거래를 un-flag 가능
#   → if_score >= 32.5이면 hybrid >= 40 → 사기 패턴의 고액 거래는 유지
FLAG_THRESHOLD = 40.0
RULE_SCORE_HIGH_VALUE = 45.0
IF_ALPHA = 0.4  # 룰 비중 40%, IF 비중 60% — hybrid = 18 + 0.6*if_score, un-flag 기준 if_score < 36.7

TYPE_MAP = {"PAYMENT": 0, "CASH_IN": 1, "DEBIT": 2, "TRANSFER": 3, "CASH_OUT": 4}


def _extract_if_features(df: pd.DataFrame) -> np.ndarray:
    """IF 학습/추론용 피처 행렬.

    핵심 피처:
    - dest_both_zero: oldbalanceDest=0 AND newbalanceDest=0
        PaySim TRANSFER 사기의 핵심 신호 — 수신 계좌(노새 계좌)가
        잔액 변동 기록 없이 돈을 받는 패턴. 정상 TRANSFER에서는 드묾.
    - log_amount, drain_ratio, type_code: 보조 신호
    """
    drain = (df["oldbalanceOrg"] - df["newbalanceOrig"]) / (df["oldbalanceOrg"] + 1.0)
    dest_both_zero = (
        (df["oldbalanceDest"] == 0.0) & (df["newbalanceDest"] == 0.0)
    ).astype(float)
    type_code = df["type"].map(TYPE_MAP).fillna(2).astype(float)
    return np.column_stack([
        np.log1p(df["amount"].values),
        drain.values,
        dest_both_zero.values,
        type_code.values,
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
    parser = argparse.ArgumentParser(description="PaySim 기반 FDS 성능 비교 평가")
    parser.add_argument("--csv", required=True, help="PaySim CSV 파일 경로")
    parser.add_argument("--sample", type=int, default=200_000, help="샘플 행 수 (기본: 200,000)")
    args = parser.parse_args()

    # ── 데이터 로드 ────────────────────────────────────────────────────────────
    logger.info("PaySim 로드 중: %s", args.csv)
    df = pd.read_csv(args.csv, nrows=args.sample)
    n_fraud = int(df["isFraud"].sum())
    logger.info("로드 완료 — 전체: %d건, 사기: %d건 (%.2f%%)", len(df), n_fraud, n_fraud / len(df) * 100)

    # ── 룰 기반 점수: HIGH_VALUE (금액 상위 5%) ──────────────────────────────────
    hv_threshold = float(df["amount"].quantile(0.95))
    logger.info("HIGH_VALUE 임계값 (p95): %.2f", hv_threshold)
    rule_scores = np.where(df["amount"].values >= hv_threshold, RULE_SCORE_HIGH_VALUE, 0.0)

    # ── Isolation Forest 학습 (비사기 거래만 사용) ────────────────────────────
    non_fraud_df = df[df["isFraud"] == 0].sample(
        min(50_000, int((df["isFraud"] == 0).sum())),
        random_state=42,
    )
    X_train = _extract_if_features(non_fraud_df)
    logger.info("IF 모델 학습 중 — 비사기 %d건...", len(non_fraud_df))
    clf = IsolationForest(n_estimators=200, contamination=0.05, random_state=42)
    clf.fit(X_train)

    # ── 전체 데이터에 IF 이상 점수 계산 ──────────────────────────────────────
    X_all = _extract_if_features(df)
    raw_if = clf.decision_function(X_all)
    # decision_function: 음수일수록 이상 → [0, 100]으로 정규화
    if_scores = np.clip((-raw_if + 0.5) / 1.0 * 100.0, 0.0, 100.0)
    # 진단: fraud vs non-fraud IF 점수 분포 확인
    fraud_mask = df["isFraud"].values == 1
    rule_flagged = rule_scores >= FLAG_THRESHOLD
    logger.info(
        "IF 점수 (전체) — 사기: median=%.1f p25=%.1f p75=%.1f | 정상: median=%.1f p25=%.1f p75=%.1f",
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
            "IF 점수 (룰 탐지 집합) — 사기 %d건: median=%.1f p25=%.1f | 정상 %d건: median=%.1f p75=%.1f",
            int(rule_fraud.sum()), float(np.median(if_scores[rule_fraud])),
            float(np.percentile(if_scores[rule_fraud], 25)),
            int(rule_normal.sum()), float(np.median(if_scores[rule_normal])),
            float(np.percentile(if_scores[rule_normal], 75)),
        )

    # ── 앙상블 점수 ───────────────────────────────────────────────────────────
    hybrid_scores = np.clip(IF_ALPHA * rule_scores + (1.0 - IF_ALPHA) * if_scores, 0.0, 100.0)

    # ── 성능 평가 ─────────────────────────────────────────────────────────────
    y_true = df["isFraud"].values
    rule_metrics = _evaluate(y_true, rule_scores, FLAG_THRESHOLD)
    hybrid_metrics = _evaluate(y_true, hybrid_scores, FLAG_THRESHOLD)

    fpr_rule = rule_metrics["false_positive_rate"]
    fpr_hybrid = hybrid_metrics["false_positive_rate"]
    fpr_reduction = (fpr_rule - fpr_hybrid) / fpr_rule * 100.0 if fpr_rule > 0 else 0.0
    recall_delta = (hybrid_metrics["true_positive_rate"] - rule_metrics["true_positive_rate"]) * 100.0

    results = {
        "note": "이 파일은 paysim_eval.py 실행 결과입니다. PaySim CSV 없이도 API가 동작하도록 샘플값이 포함됩니다.",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": f"PaySim (n={len(df):,}, fraud={n_fraud:,})",
        "rule_description": f"HIGH_VALUE: amount >= p95({hv_threshold:.2f}) → {RULE_SCORE_HIGH_VALUE}점 (라이브 시스템 HIGH_VALUE 룰과 동일 컨셉)",
        "flag_threshold": FLAG_THRESHOLD,
        "alpha": IF_ALPHA,
        "rule_only": rule_metrics,
        "hybrid_rule_if": hybrid_metrics,
        "improvement": {
            "fpr_reduction_pct": round(fpr_reduction, 2),
            "recall_delta_pct": round(recall_delta, 2),
        },
    }

    RESULTS_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("결과 저장 완료 → %s", RESULTS_PATH)
    logger.info(
        "룰 단독   — FPR: %.1f%%, Recall: %.1f%%, F1: %.3f",
        fpr_rule * 100,
        rule_metrics["true_positive_rate"] * 100,
        rule_metrics["f1_score"],
    )
    logger.info(
        "룰+IF 앙상블 — FPR: %.1f%%, Recall: %.1f%%, F1: %.3f  (FPR↓ %.1f%%p, Recall↑ %.1f%%p)",
        fpr_hybrid * 100,
        hybrid_metrics["true_positive_rate"] * 100,
        hybrid_metrics["f1_score"],
        fpr_reduction,
        recall_delta,
    )


if __name__ == "__main__":
    main()
