# FDS 성능 평가 파이프라인

룰 기반 FDS(HIGH_VALUE 단독)와 룰+Isolation Forest 앙상블의 성능을 공개 데이터셋으로 비교한다.

## 실험 목적

라이브 시스템의 HIGH_VALUE 룰이 떠올린 **후보군 안에서**, IF가 거짓경보(false alarm)를 얼마나 걷어내는지 정량화한다.  
주장: "사기를 더 잘 잡는다"가 아니라 **"탐지율은 거의 안 깎으면서 운영팀 검토 부담을 절반으로 줄인다"**.

## 결과 (ULB Credit Card Fraud, n=284,807, fraud=492)

| 방식 | FPR | Recall | 탐지 건수 | 거짓 경보 |
|------|-----|--------|----------|---------|
| HIGH_VALUE 룰 단독 | 4.99% | 8.74% | 14,242건 | 14,199건 |
| 룰 + IF 앙상블 (α=0.4/0.6) | **2.50%** | 8.33% | 7,137건 | **7,096건** |
| 변화 | **▼50.0%** | ▼0.41%p | — | **7,103건 감소** |

거짓경보 7,103건 제거 / 탐지율 손실 0.41%p.

## 방법론

- **룰**: `Amount >= p95($365)` → rule\_score = 45 (라이브 HIGH\_VALUE 룰과 동일 컨셉)
- **IF**: V1-V28(PCA) + log\_amount 29개 피처, 비사기 거래 50,000건으로 학습
- **앙상블**: `hybrid = 0.4 × rule_score + 0.6 × if_score`, 임계값 40.0
  - `if_score < 36.7` → 룰이 잡은 고액 정상 거래 해제 (FPR↓)
  - 룰 미탐지 + `if_score >= 66.7` → 비고액 사기 추가 포착 가능 (Recall↑ 효과 미미)

## 한계 및 유의사항

**Recall 표본 크기**: 룰 후보군 내 사기가 43건으로 작다. 앙상블 후 41건(2건 차이)으로 Recall -0.41%p.  
3건 이내 차이는 통계적으로 견고하지 않으므로 **recall 변화는 참고치**로 본다. 핵심 효과는 FPR 쪽.

**Recall 절댓값(8.74%)에 대해**: HIGH_VALUE 룰 하나만 사용했기 때문에 고액이 아닌 사기는 후보에 올라오지 않는다.  
이 실험은 라이브 시스템 전체 Recall이 아니라, HIGH_VALUE 후보군 내 IF 효과만 측정한다.  
라이브 시스템은 FAILURE_RATE, VELOCITY, LOGIN_FAILURE, LATENCY 룰을 병용한다.

## 데이터셋 선택 이유 (PaySim → Credit Card 교체)

PaySim으로 먼저 시도했으나 아래 이유로 교체했다.

```
진단 (evaluation/paysim_eval.py 실행 결과):
  룰 탐지 집합 IF 점수 — 사기 p25=45.1 ≈ 정상 p75=46.9 (거의 완전 중첩)
```

고액 거래는 사기·정상 무관하게 IF가 이상치로 분류해 변별력이 없었다.  
Credit Card Fraud의 V1-V28(PCA 피처)는 사기 패턴이 직접 인코딩되어 룰 신호(금액)와 직교하므로 앙상블 효과가 나타났다.

```
진단 (evaluation/creditcard_eval.py 실행 결과):
  룰 탐지 집합 IF 점수 — 사기 median=49.8  p25=42.2 | 정상 median=36.7  p75=40.9
```

## 재실행 방법

```bash
# Credit Card (실제 평가)
python evaluation/creditcard_eval.py --csv evaluation/data/creditcard.csv

# PaySim (실패 재현 / 참고용)
python evaluation/paysim_eval.py --csv evaluation/data/PS_20174392719_1491204439457_log.csv
```

결과는 `evaluation/results.json`에 저장되며 `GET /fds/comparison` API로 반환된다.
