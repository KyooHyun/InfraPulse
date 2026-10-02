# FDS 성능 평가

## 평가 방법 — 운영 엔진으로 잰다

```bash
python scripts/train_model.py --source paysim --csv evaluation/data/PS_20174392719_1491204439457_log.csv
python scripts/evaluate.py    --source paysim --csv evaluation/data/PS_20174392719_1491204439457_log.csv
```

평가 스크립트는 점수를 직접 계산하지 않는다. 모든 구성 요소를 운영 코드에서 import한다.

| 구성 요소 | 정의 위치 (유일) |
|---|---|
| 룰 점수 | `app.fds_engine.evaluate_signals` |
| 룰 임계값·가중치 | `app.fds_engine.DEFAULT_RULES` |
| ML 피처 | `app.ml.features.feature_values` |
| IF 원점수 → 0~1 정규화 | `app.ml.isolation_forest.IFModel.anomaly_scores` |
| 앙상블과 α | `app.ml.ensemble.ensemble_score`, `RULE_ALPHA = 0.5` |

신호와 피처의 **원천값을 모으는 쪽**만 둘로 나뉜다. 운영은 거래마다 DB를 조회하고
(`collect_signals`, `extract_features`), 평가는 277만 건을 한 번에 계산한다(`calibration/dataset.py`).
`tests/test_scoring_parity.py`는 같은 거래 300건을 양쪽에 넣어 신호, 룰 점수, ML 피처가 모두
일치하는지 확인한다. 이력에는 동률 시각, 윈도우 경계, 실패율 표본 부족 같은 경계 조건을 일부러 넣었다.
`tests/test_single_source.py`는 위 표의 구성 요소가 다른 파일에 다시 정의되면 실패한다.

## 재측정 결과 (PaySim TRANSFER/CASH_OUT, 2,770,409건, 사기 8,213건 = 0.296%)

| 방법 | ROC-AUC | PR-AUC | recall@0.1% | recall@0.5% | recall@1% |
|---|---:|---:|---:|---:|---:|
| 룰 단독 | 0.547 | 0.0033 | 0.001 | 0.006 | 0.011 |
| Isolation Forest | 0.883 | 0.0354 | 0.018 | 0.159 | 0.217 |
| 앙상블 (α=0.5) | 0.794 | 0.0398 | 0.018 | 0.173 | 0.243 |

recall@k%는 점수 상위 k%만 알림으로 보낼 때의 재현율이다. 무작위로 골랐을 때의 기댓값은
k%와 같다(예: recall@0.5%의 기댓값은 0.005). 경계에 걸린 동점 묶음은 그 안에서 무작위로 고를 때의
기댓값으로 셌다.

현행 등급 경계(40/70)에 걸리는 거래:

| 방법 | MEDIUM | HIGH |
|---|---:|---:|
| 룰 단독 | 0 | 0 |
| 앙상블 (α=0.5) | 53,762 | 0 |

**기존 진단이 재현됐다.** 룰 단독 AUC 0.547과 MEDIUM/HIGH 0건은 리팩터링 전 calibration 결과와
같다. calibration은 처음부터 `evaluate_signals`와 `DEFAULT_RULES`를 썼기 때문에, 이 수치는 실제
룰 엔진으로 잰 값이었다. 엔진을 따로 구현해 쓴 곳은 철회한 ULB·PaySim 평가 스크립트 두 개뿐이었다.
이 표가 앞으로 개선할 때의 기준점이다.

### 해석할 때 주의할 점

- **룰 단독은 무작위와 같다.** recall@0.5%가 0.006으로, 무작위 기댓값 0.005와 거의 같다.
  PaySim에서 실제로 동작하는 룰은 HIGH_VALUE(10만, 전체의 69.8%에서 발화)뿐이고, 점수가 가질 수 있는
  값이 {0, 30} 둘뿐이다.
- **IF 수치는 낙관적이다.** 같은 데이터로 학습하고 평가했다(표본 내 평가). 시간 분할 평가를 넣은 뒤에
  다시 재야 한다.
- **IF 피처에 거래 후 정보가 들어 있다.** 9개 피처 중 `balance_orig_after`, `balance_dest_after`,
  `error_orig`, `error_dest`는 이체가 끝난 **뒤**의 잔액으로 계산한다. 운영에서도 ML 점수는 이체를
  기록한 뒤에 매기므로, 지금 구조로는 ML이 이체를 막을 수 없다. 또 PaySim에서 이 잔액 불일치
  피처는 합성 데이터의 특성(사기 거래의 잔액 기록 방식)과 관련이 깊어서 일반화할 수 없을 가능성이
  높다. 거래 전 정보만 쓰는 피처로 다시 재는 것이 다음 작업이다.
- **앙상블의 ROC-AUC(0.794)가 IF 단독(0.883)보다 낮다.** 반면 PR-AUC와 알림 예산 재현율은 조금
  높다. 무정보에 가까운 룰 점수({0, 30})를 절반 가중치로 섞으면서 전체 순위는 흐려지지만, 상위
  구간은 크게 바뀌지 않기 때문이다. "앙상블이 낫다"고 말하려면 시간 분할 평가로 다시 확인해야 한다.
- **앙상블도 HIGH(70점)에는 도달하지 못한다.** PaySim에서 룰 점수의 상한은 40점(HIGH_VALUE 30 + VELOCITY 10)이고,
  α=0.5에서 앙상블 상한은 0.5·0.4 + 0.5·1.0 = 70점이다. IF 점수가 정확히 1.0인 거래만 경계에
  닿는데, 그런 거래가 없었다.

## 이전 결과 철회

이 디렉터리에는 ULB Credit Card Fraud 데이터셋(n=284,807, fraud=492)으로 "룰 단독 vs
룰+Isolation Forest 앙상블"을 비교해 **FPR ▼50%, Recall ▼0.41%p**라고 적은 결과가 있었다.
이 수치는 철회했고, 해당 스크립트(`creditcard_eval.py`, `paysim_eval.py`)는 삭제했다. 철회 이유:

1. **기준선이 실제 엔진이 아니었다.** 두 스크립트는 `app/fds_engine.py`를 호출하지 않고
   `Amount >= p95 → 45점` 룰을 스크립트 안에서 따로 구현했다. 실제 HIGH_VALUE는 30점이라
   단독으로는 MEDIUM(40점)에도 못 미친다.
2. **α와 IF 정규화가 운영과 달랐다.** 두 스크립트 모두 `IF_ALPHA = 0.4`를 썼다(paysim_eval.py의
   docstring에는 0.6이라고 적혀 있어 문서와 코드도 어긋나 있었다). 운영은 0.5다. IF 원점수를
   0~100으로 바꾸는 정규화도 스크립트마다 따로 구현했다.
3. **비교 방식이 주장을 뒷받침하지 못한다.** FPR 절반 감소는 임계값만 올려도 얻을 수 있다.
   Recall 8.74%는 사기 492건 중 약 43건을 잡았다는 뜻이기도 하다.

`GET /fds/comparison`은 시간 분할 평가 결과가 나올 때까지 `{"status": "pending"}`을 반환한다.
위 표는 표본 내 평가라서 API로 공개하지 않는다.
