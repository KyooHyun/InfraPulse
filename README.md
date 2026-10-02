# FDS — 금융 이상거래 탐지 시스템

금융권 포트폴리오용 **FDS(이상금융거래탐지시스템)** 구현체입니다.  
한국 금융 규정을 기반으로 이상거래 탐지·검토·보고 전 과정을 구현했습니다.

---

## 규정 근거

| 기능 | 근거 법령 |
|------|-----------|
| FDS 의무 운영 | 전자금융감독규정 제37조의2 |
| STR (의심거래보고) | 특정금융정보법 제4조 |
| CTR (고액현금거래보고) — **미구현**, 아래 참고 | 특정금융정보법 제4조의2 |
| KYC (고객확인제도) | 특정금융정보법 제5조의2 |
| 감사 추적 보존 | 전자금융거래법 제22조 |

---

## 시스템 아키텍처

```
                  ┌─────────────────────────────────────┐
                  │         transaction-api              │
                  │  FastAPI  │  FDS Engine  │  Reports  │
                  │  JWT/RBAC │  Rule + IF   │ STR 초안  │
                  └────┬──────────────┬──────────────────┘
                       │              │
              ┌────────▼──┐    ┌──────▼──────┐
              │   MySQL   │    │  Prometheus  │
              │ (11개 테이블)│    │  (메트릭 수집)│
              └───────────┘    └──────┬───────┘
                                      │
                               ┌──────▼───────┐
                               │    Grafana    │
                               │  (대시보드)   │
                               └──────────────┘
simulator ──────────────────────────► transaction-api
(JWT 인증 후 거래/로그인실패 시뮬레이션)
```

---
## ML / PaySim 검증 전략
- 공개 모바일 머니 거래 데이터셋 **PaySim**(Kaggle)을 유일한 외부 벤치마크로 사용합니다.
  (이전에 쓰던 ULB Credit Card 비교 결과는 철회했습니다 — 3절 참고)
- `scripts/train_model.py`는 Isolation Forest를 비지도 학습으로 학습하며, 레이블은 contamination 추정과 평가에만 사용합니다.
- `scripts/evaluate.py`는 룰 단독, Isolation Forest, 룰+ML 앙상블을 ROC-AUC·PR-AUC·**같은 알림 예산에서의 재현율**로 비교합니다.
  CSV에서 바로 읽으며(`--source paysim --csv ...`), DB 적재(`scripts/load_paysim.py`)는 선택입니다.
- **평가 코드는 점수를 직접 계산하지 않습니다.** 룰 점수(`evaluate_signals`, `DEFAULT_RULES`), IF 정규화
  (`IFModel.anomaly_scores`), 앙상블(`ensemble_score`, α=`RULE_ALPHA`)을 모두 운영 코드에서 import합니다.
  277만 건을 거래마다 DB 조회로 계산할 수는 없어서 신호·피처를 모으는 쪽만 오프라인 구현
  (`calibration/dataset.py`)이 따로 있고, 운영 경로와 같은 값을 내는지는 `tests/test_scoring_parity.py`가
  고정합니다. α·IF 생성·정규화·룰 기본값이 정해진 한 곳 밖에 다시 정의되면 `tests/test_single_source.py`가 실패합니다.
- PaySim 평가 시에는 CSV에 포함된 `TRANSFER`/`CASH_OUT` 거래와 PaySim에 존재하는 룰 피처만 사용합니다. 로그인 실패, 거래 실패율, 응답 지연과 같은 항목은 PaySim 원본 데이터에 직접 포함되지 않아 별도 평가 대상에서 제외됩니다.
- **ML 앙상블은 기본으로 꺼져 있습니다.** `FDS_ML_ENABLED=true`로 명시해야 켜집니다. 모델 파일이 있다고
  자동으로 켜지지 않습니다 — PaySim(USD)으로 학습한 모델이 원화 거래를 채점하면 점수에 의미가 없기 때문입니다.
- **ML 점수는 사후 모니터링입니다.** 이체를 기록한 뒤에 채점하므로 이체를 보류시키지 못합니다. 피처는 거래 전
  정보만 쓰므로 커밋 전에 채점해 보류시키는 것도 가능하지만, 이체 지연이 늘어나므로 동기/비동기 여부는
  부하 테스트에서 결정합니다.
- 알림별 `rule_contributions`와 ML `z-score` 기반 상위 이상 피처를 함께 제공해 설명가능성을 확보합니다.
- Autoencoder는 작은 PaySim 샘플과 튜닝 리스크를 고려해 현재 구현 범위에 포함하지 않습니다.
## 핵심 기능

### 1. JWT 기반 RBAC 인증
JWT(HS256) 토큰 발급 및 역할 기반 접근 제어.

| 역할 | 권한 |
|------|------|
| `STAFF` | 거래 생성·조회, KYC 등록 |
| `RISK_OFFICER` | FDS 알림 검토, 컴플라이언스 보고서 제출, KYC 승인 |
| `ADMIN` | 전체 권한 + FDS 룰 관리 + 감사 로그 조회 + 사용자 관리 |

### 1-1. 이체 동시성 제어 (계좌 원장 + 행 잠금)

이체는 "읽고 → 판단하고 → 쓰는" 연산이다. 잔액 10만원 계좌에 8만원 이체 두 건이
동시에 들어오면 둘 다 "잔액 10만원"을 읽고 둘 다 통과시켜, 잔액이 음수가 되거나
한쪽 차감이 다른 쪽 쓰기에 덮여 사라진다(lost update). 애플리케이션 레벨 `if` 문으로는
막을 수 없다 — 두 요청 모두 자기 시점에서는 조건을 만족하기 때문이다.

```python
# app/ledger.py — 데드락 방지를 위해 항상 account_id 오름차순으로 잠근다
for account_id in sorted(set(account_ids)):
    db.query(models.Account).filter(...).with_for_update().one_or_none()
```

- **`SELECT ... FOR UPDATE`** 로 계좌 행에 배타 잠금. 읽기·판단·쓰기가 한 트랜잭션에
  들어가므로 중간 상태가 다른 요청에 보이지 않는다.
- **잠금 순서 고정**: A→B와 B→A 이체가 각자 출금 계좌부터 잠그면 서로를 기다리며
  데드락이 된다. 출금/입금 구분 없이 계좌번호 오름차순으로 잠가 순환 대기를 없앤다.
- **원장 금액은 `NUMERIC(18,2)`**: 이진 부동소수는 0.1을 정확히 표현하지 못해 잔액을
  더하고 빼는 과정에서 오차가 누적된다. 합계가 맞아야 하는 테이블이므로 십진 고정소수를 쓴다.
- **잔액 변경과 거래 기록을 한 커밋으로**: 따로 커밋하면 "잔액은 줄었는데 거래 기록이
  없는" 상태가 존재할 수 있다.

**검증** — `tests/test_concurrency.py`가 같은 계좌에 이체 12건을 동시에 던진다.
잔액이 3건분뿐이므로 성공은 최대 3건이어야 하고, 줄어든 잔액이 성공 건수와 정확히
일치해야 한다. 이 테스트가 의미 있다는 근거는 **꺼보면 깨진다**는 것이다:

```
$ FDS_TEST_NO_LOCK=1 pytest tests/test_concurrency.py
AssertionError: 잔액으로 감당 가능한 건수(3)보다 많이 성공했다: 11
```

> 마지막 숫자는 돌릴 때마다 달라진다 — 어떤 요청이 어느 시점에 잔액을 읽었는지에
> 달렸기 때문이다. 경합 버그가 재현되지 않는다고 없는 게 아니라는 점이 여기서 보인다.

> 운영 DB는 MySQL이라 `FOR UPDATE`가 실제 행 잠금으로 동작한다. 테스트용 SQLite는
> 행 잠금이 없어 SQLAlchemy가 이 구문을 조용히 버리므로, `conftest.py`가
> `BEGIN IMMEDIATE`로 같은 직렬화 효과를 만든다. `FOR UPDATE` 구문이 실제로 SQL에
> 실린다는 것은 `test_lock_query_emits_for_update`가 따로 검증한다.

**멱등성 키 — 재전송돼도 출금은 한 번** (`app/idempotency.py`, `Idempotency-Key` 헤더)

네트워크 타임아웃 뒤 클라이언트가 같은 이체를 다시 보내면, 잠금은 두 요청을 차례로 실행할 뿐이라
두 번 출금된다. 그래서 이체마다 고유한 키를 받아 처음 결과를 기억한다.

- **원자성**: 키 행은 잔액 변경·거래 기록과 **같은 커밋**에 들어간다. 처리 중 예외로 롤백되면 키도 남지 않아
  같은 키로 다시 시도할 수 있다.
- **동시 요청**: 보장은 `(user_id, key)` 유니크 제약이다. 같은 키의 요청이 동시에 오면 하나만 커밋되고, 나머지는
  제약 위반 → 롤백(잔액 변경도 함께) → 먼저 확정된 거래를 돌려받는다. 잠금 전·후의 사전 조회는 최적화일 뿐이다
  — MySQL REPEATABLE READ에서는 스냅샷 때문에 방금 커밋된 키가 조회에 안 보일 수 있다.
- **키 재사용**: 계좌·금액·통화의 해시를 함께 저장한다. 같은 키로 내용이 다른 요청은 409.
- **확정된 실패도 저장한다**: 잔액 부족·대외계 실패도 거래로 기록되므로, 나중에 잔액이 생겨도 같은 키는 같은
  실패를 돌려준다. 재시도로 결과가 바뀌면 클라이언트는 처음 요청의 결과를 알 수 없다.
- 재현된 응답에는 `Idempotent-Replayed: true` 헤더가 붙는다. 키는 선택이다(없으면 매 요청이 새 이체) — 운영
  클라이언트는 반드시 보내야 한다.

**검증** — `tests/test_idempotency.py`: 같은 키로 12건을 동시에 보내면 출금은 정확히 1건, 응답은 12건 모두
같은 거래다. 대조군 두 개로 이것이 무엇 때문에 성립하는지 보인다.
  - 키 기록을 끄면 같은 이체가 **12번** 출금된다 (경합이 실제로 일어났다는 증거)
  - 사전 조회를 전부 무력화해도 1건이다 (보장이 조회가 아니라 유니크 제약에서 나온다는 증거)

**실제 MySQL에서 검증** (Testcontainers, MySQL 8.0 — REPEATABLE READ)

위 SQLite 테스트는 `BEGIN IMMEDIATE`로 DB 전체를 직렬화해 행 잠금을 흉내낸 것이다. `FDS_TEST_DB=mysql`이면 같은
테스트가 운영과 같은 MySQL 컨테이너 위에서 **그대로** 돈다. 여기서 처음으로 `FOR UPDATE` 주장이 실제 엔진에서 확인됐다.

| 확인한 것 | 결과 |
|---|---|
| 동시 이체 12건 — 초과 인출 없음, 금액 보존 (`test_concurrency.py`) | 통과 |
| 같은 멱등성 키 동시 12건 — 출금 1건 (`test_idempotency.py`, REPEATABLE READ 스냅샷에서도 유니크 제약이 보장) | 통과 |
| **대조군: FOR UPDATE를 빼면** 같은 시나리오에서 초과 인출이 일어난다 | 재현됨 |
| **데드락: 계좌 정렬을 끄고** A→B·B→A를 교차로 잠그면 MySQL이 1213으로 하나를 죽인다 / 정렬하면 데드락 없음 | 재현됨 / 없음 |
| 실제 데드락이 난 이체 — 재시도로 두 건 모두 성공, 금액 보존 | 통과 |
| 다른 트랜잭션이 계좌를 쥐고 있으면 잠금 대기 초과(1205) → 재시도 → 503, 출금 없음 → 풀린 뒤 같은 키로 성공 | 통과 |
| 테스트 전체(117건) | 통과 |

- **잠금 충돌 처리**: 데드락(1213)·잠금 대기 초과(1205)는 MySQL이 트랜잭션을 롤백한 것이라, 처음부터 다시 실행해도
  이중 출금이 없다. 지수 백오프로 최대 3회 재시도(`TRANSFER_LOCK_RETRIES`)하고, 그래도 안 되면 503을 돌려준다 —
  클라이언트는 같은 멱등성 키로 다시 보내면 된다. 잠금 대기 상한은 InnoDB 기본 50초 대신 5초
  (`MYSQL_LOCK_WAIT_TIMEOUT_SECONDS`). 재시도 횟수는 `transfer_lock_retry_total{reason}` 메트릭으로 남는다.
- **MySQL에서 처음 드러난 버그 — 거래 금액이 잘려 기록됐다.** 거래·보고서의 금액·잔액 컬럼이 SQLAlchemy `Float`였는데,
  MySQL에서는 단정밀도 FLOAT이고 유효숫자 6자리로 돌아온다. **1,234,567원 → 1,234,570원, 12,345.67원 → 12,345.70원.**
  SQLite는 8바이트 실수라 지금까지 모든 테스트가 통과했다. 원장(`accounts.balance`)은 처음부터 `NUMERIC(18,2)`라 잔액
  자체는 정확했지만, 거래 기록과 STR 보고서의 금액이 틀렸다. 금액은 `NUMERIC(18,2)`, 점수·임계값은 `DOUBLE`로 바꿨다
  (`tests/test_money_precision.py` — `Float`로 되돌리면 MySQL에서 실패함을 확인). 기존 DB는
  `scripts/migrations/2026-10-03_money_columns_decimal.sql`로 바꾸되, 이미 잘린 값은 복구되지 않는다.
- **부하 테스트 때 측정할 것**: 감사 로그 해시 체인은 모든 기록이 `audit_chain_head` 한 행을 `FOR UPDATE`로 거쳐 간다.
  이체마다 감사 로그를 남기므로 MySQL에서 전역 직렬화 지점이 된다. 지금은 고치지 않고 병목 여부를 잰다.

### 2. DB 기반 FDS 룰 엔진
임계값·가중치를 DB에서 관리하여 서비스 재시작 없이 변경 가능. 모든 룰은 **이체 실행 전에 알 수 있는 정보**만 쓴다.

| 룰 유형 | 임계값 | 점수 | 근거 (PaySim 1~9일 lift) |
|---------|--------|-----:|------|
| `HIGH_VALUE` (고액, 금액 상위 5%) | 827,513 | +20 | 7.3 |
| `HIGH_VALUE_TOP` (초고액, 상위 1% — 위에 추가) | 1,775,460 | +10 | 21.3 |
| `BALANCE_DRAIN` (거래 전 잔액의 90~100% 인출, 잔액 > 0) | 0.9 | +45 | 103.5 |
| `DEST_EMPTY` (수취 계좌 거래 전 잔액 0) | — | +15 | 4.8 |
| `NEW_RECIPIENT` (수취 계좌가 직전 24시간 입금 없음) | — | +10 | 2.4 |
| `VELOCITY` (동일 송금 계좌 10분 내 5건 이상) | 5건 | +10 | PaySim에서 측정 불가 — 기존 판단치 유지 |
| `FAILURE_RATE` / `LOGIN_FAILURE` / `LATENCY` | — | 0 | 시스템 모니터링 신호 — 거래 점수에 기여하지 않음 |

- **가중치 = 10·ln(lift)** (5점 단위 반올림, `python -m calibration.weights`). lift가 곱해지면 점수는 더해지는 로그 오즈
  척도라, 40점(MEDIUM)은 lift 약 55배, 70점(HIGH)은 약 1,100배에 해당한다. 룰 사이의 상관은 무시한다(나이브 베이즈 가정).
- **등급 경계 40/70은 유지했다.** 위 척도에서 의미가 분명하고(위험의 크기), 검토 물량은 등급이 아니라 알림 예산
  (점수 상위 N%)으로 조절하는 것이 맞다고 판단했다. 실제로 평가 구간에서 MEDIUM 이상은 거래의 3.4%라 예산(0.5%)보다
  훨씬 많다 — 대기열은 점수순으로 처리해야 한다.
- **HIGH는 BALANCE_DRAIN이 있어야 도달한다.** 단독으로는 45점(MEDIUM)이고, 다른 신호가 25점 이상 겹쳐야 HIGH다.
  HIGH에 도달하는 모든 조합에 이 룰이 들어 있다는 사실은 `test_high_requires_balance_drain`이 고정한다.
- **주의 — 가중치와 임계값은 PaySim 기준이다.** PaySim의 사기는 잔액을 정확히 비우는 시뮬레이터 특성이 있어
  BALANCE_DRAIN의 lift가 크게 나온다. 실제로는 본인 계좌 간 이동이나 계좌 정리처럼 잔액을 전부 옮기는 정상 거래가
  흔하므로, **원화 운영 데이터로 재보정하기 전에는 이 가중치를 그대로 쓰면 안 된다.** 금액 단위도 원화가 아니다.
- 알림은 **MEDIUM 이상일 때만** 만든다. 예전에는 점수와 무관하게 발화한 룰마다 알림을 만들었다.

**위험 등급:**
- `LOW` (0~39점): 기록 및 모니터링
- `MEDIUM` (40~69점): FDS 알림 생성, 담당자 검토 대기
- `HIGH` (70~100점): FDS 알림 생성 + STR(의심거래보고서) 초안 생성 → 담당자 검토

**이전 룰의 진단** (`calibration/`, PaySim 2,770,409건): 예전 룰(HIGH_VALUE 10만 +30, FAILURE_RATE +25,
VELOCITY +10, LOGIN_FAILURE·LATENCY는 가중치만 있고 점수에 기여 못 함)은 **룰 점수 AUC 0.547로 무작위 수준**이었다.
점수가 {0, 30} 둘뿐이라 MEDIUM/HIGH 거래가 0건이었고, 룰만으로는 HIGH에 구조적으로 도달할 수 없었다. HIGH_VALUE
10만은 거래의 69.8%에서 발화했다(lift 1.14). FAILURE_RATE는 전체 거래 실패율이라 개별 거래의 사기 여부와 무관한
시스템 신호였다. 송금 계좌가 최대 3회만 등장해(2,768,630개 중 1,776개가 2회 이상) VELOCITY는 측정할 수 없었다.
반면 수취 계좌는 509,565개 중 201,944개가 5회 이상 등장해 수취인 기준 신호는 측정할 수 있다. 다만 예상과 달리
"입금을 많이 받는 수취인"은 오히려 안전했고(lift 0.2~0.3), 위험 신호는 **입금이 없던 신규·휴면 수취인**이었다.

### 3. 룰 + Isolation Forest 앙상블 — 최종 비교

Isolation Forest(비지도)로 거래별 이상 점수를 내고 룰 점수와 가중 앙상블한다. α=0.5 — 운영 값을 그대로 썼고,
데이터로 다시 고르지 않았다.

```
ensemble = α × rule_score + (1-α) × if_score × 100
```

**평가 설계** (PaySim, 운영 엔진의 점수 함수를 그대로 import):
- 룰 가중치·임계값 결정 1~9일 → 검증 10~12일 → **평가 13~17일은 마지막에 한 번만 열었다.** 연 뒤에는 아무것도
  바꾸지 않았다. IF는 13~17일에서 이전 단계(시간 분할·피처 교체)에 이미 측정했으므로 재측정이다(피처 선택은 검증 구간에서).
- IF 학습과 알림 임계값(학습 구간 점수 상위 0.5%)은 1~12일로 정했다. IF는 시드 5개 평균±표준편차.
- PaySim의 금액 단위·잔액 기록 방식은 합성 데이터 특성이다. 아래는 "이 구조가 이 데이터에서 어떻게 동작하는가"이지
  원화 운영 환경의 성능이 아니다.

**평가 13~17일** (사기 비율 0.148%, 886,056건). 재현율@0.5% = 모든 방법이 같은 알림 건수(상위 0.5%)일 때의 재현율.
경계에 걸린 동점 묶음은 그 안에서 무작위로 고를 때의 기댓값으로 셌다(룰 점수는 값의 종류가 적어 동점이 많다).
**대표 수치는 BALANCE_DRAIN 제외 기준이다.**

| 방법 | ROC-AUC | PR-AUC | 재현율@0.5% |
|---|---:|---:|---:|
| 이전 룰 (참고: AUC 0.545) | 0.545 | 0.0016 | — |
| 룰 | 0.997 | 0.526 | 0.803 |
| 룰 (BALANCE_DRAIN 제외) | 0.810 | 0.015 | 0.164 |
| Isolation Forest | 0.906 ± 0.003 | 0.145 ± 0.029 | 0.341 ± 0.012 |
| 앙상블 | 0.992 ± 0.000 | 0.565 ± 0.013 | 0.636 ± 0.021 |
| 앙상블 (BALANCE_DRAIN 제외) | 0.918 ± 0.002 | 0.291 ± 0.007 | 0.353 ± 0.002 |

- **룰은 무작위 수준을 벗어났다 — BALANCE_DRAIN을 빼도.** 제외 시 ROC-AUC 0.810, 같은 예산 재현율 0.164
  (무작위 0.005의 33배). 이전 룰은 0.545였다.
- **BALANCE_DRAIN을 넣은 수치(재현율 0.803)는 PaySim 특성에 기댄 값이다.** 합성 데이터에서 사기는 잔액을 정확히
  비운다. 이 수치를 성능으로 주장하지 않는다.
- **앙상블이 IF보다 낫다 — 순위 전체(PR-AUC)에서는.** DRAIN 제외 기준 PR-AUC 0.291 vs 0.145로, 차이가 시드 편차
  (0.029)보다 훨씬 크다. **하지만 예산 0.5% 지점의 재현율은 0.353 vs 0.341로, 차이가 IF의 시드 편차(0.012) 수준이라
  구분할 수 없다.** 검토 인력이 정해진 운영 관점에서는 "앙상블이 IF보다 낫다"고 말할 근거가 아직 없다.
- **DRAIN을 넣으면 앙상블이 룰 단독보다 나쁘다** (재현율@0.5% 0.636 vs 0.803). 강한 룰 점수에 IF를 절반 섞으면 순위가 흐려진다.

**알림 비율 — 예산 0.5%** (IF, 평가 13~17일):

| 임계값 방식 | 실제 알림 비율 | 날짜별 편차 평균 | 재현율 |
|---|---:|---:|---:|
| 고정 (학습 구간 상위 0.5%) | 0.926% | 0.416%p | 0.402 |
| 직전 1일 분위수로 매일 갱신 | 0.582% | 0.253%p | 0.366 |
| 직전 7일 분위수로 매일 갱신 | 0.810% | 0.405%p | 0.390 |

- 직전 1일 분위수로 임계값을 매일 다시 정하면(레이블 불필요, `app/alerting.py`) 알림 비율이 예산에 가까워진다.
  앙상블은 1.309% → 0.559%, 18~31일(저거래 구간)에서는 2.681% → 0.619%다. 대가로 재현율이 내려간다 — 알림을 덜 보내기 때문이다.
- 다만 **검증 구간(10~12일)에서는 효과가 없었다.** 거기서는 고정 임계값이 이미 0.41%로 예산 안이었고, 직전 1일
  방식은 0.63%였다. 정리하면 고정 임계값은 구간에 따라 0.41~2.7%로 예측할 수 없고, 직전 1일 방식은 세 구간 모두
  0.53~0.69%(IF·앙상블)로 안정적이다. 7일 창은 분포가 바뀔 때 따라가지 못했다. **창 크기는 사전에 정하지 않았다** —
  1·3·7일을 모두 돌려 실었고, 1일 창이 낫다는 판단은 평가 구간을 보고 한 것이다.
- **알림 비율 안정은 검토 인력을 보호하는 장치이지 탐지 개선이 아니다.** 사기가 몰리는 18~31일에서 직전 1일 방식은
  앙상블 재현율을 0.798 → 0.252로 떨어뜨렸다.

**참고 — 18~31일** (사기 비율 2.344%, 성격이 전혀 다른 구간): 룰 (DRAIN 제외) PR-AUC 0.199, IF 0.464 ± 0.013,
앙상블 (DRAIN 제외) 0.501 ± 0.004. 상세는 [`evaluation/README.md`](evaluation/README.md).

이전의 ULB 비교표("FPR ▼50%")는 철회했다. 평가 스크립트가 실제 엔진 대신 `Amount >= p95 → 45점` 룰을 따로 구현해
기준선으로 썼고, α(0.4)와 IF 정규화도 운영과 달랐다. `GET /fds/comparison`은 계속 `{"status": "pending"}`이다 —
위 결과는 PaySim 기준이라 운영 API에서 "성능"으로 노출하지 않는다.

### 4. FDS 이상거래 검토 워크플로우
```
이상 감지 → DETECTED → (담당자 검토) → APPROVED(정상) / REJECTED(이상거래 확정)
```

### 5. 컴플라이언스 보고 (STR 초안 → 담당자 판단 → 제출)
```
위험점수 70점 이상 → DRAFT → (RISK_OFFICER 검토, 사유 필수) → APPROVED → SUBMITTED
                                                             └→ DISMISSED (보고 불필요)
```
- **STR**: 특금법 제4조의 요건은 "의심되는 합당한 근거"에 대한 **사람의 판단**이다. 그래서
  점수로는 초안만 만들고, 제출은 검토에서 APPROVED된 건만 할 수 있다. 검토자·시각·판단 사유는
  보고서 행(`reviewed_by`, `reviewed_at`, `review_reason`)에 저장해 바로 조회하고, 해시 체인
  감사 로그에도 별도로 남긴다(`REVIEW_STR_APPROVE` / `REVIEW_STR_DISMISS`). FDS 알림의
  `reviewed_by`/`reviewed_at`과 같은 구조다.
  70점은 ML 앙상블이 켜진 구성에서만 도달할 수 있다(2절 참고).
- **CTR — 미구현**: CTR 대상은 **현금** 입출금이고, 기준은 **동일인 1거래일 합산** 1천만원
  이상이다. 이 시스템에는 계좌이체만 있고 계좌를 고객 단위로 묶는 식별자도 없다. 예전에는
  이체 한 건이 1천만원 이상이면 CTR을 만들었는데, 제도와 다른 동작이라 제거했다. 현금 거래
  유형과 고객 식별자(KYC ↔ 계좌)를 추가할 때 구현한다.
- 각 보고서에 고유 번호 부여 (`STR-20260605-A1B2C3D4`). 실제 환경에서는 제출이 KoFIU API 연동으로 대체된다.
- 상태값 변경(PENDING → DRAFT) 이전에 만든 DB가 있다면
  [`scripts/migrations/2026-10-03_str_draft_status.sql`](scripts/migrations/2026-10-03_str_draft_status.sql)을
  한 번 실행한다. 기존 PENDING STR은 DRAFT로 되돌리고, 이체로 생성됐던 CTR은 지우지 않고 DISMISSED로 종결한다.
  검토 컬럼 추가 이전 DB라면
  [`scripts/migrations/2026-10-03_compliance_review_columns.sql`](scripts/migrations/2026-10-03_compliance_review_columns.sql)도 실행한다.

### 6. 불변 감사 추적 (Audit Trail) — SHA-256 해시 체인
모든 중요 이벤트를 `audit_logs` 테이블에 기록한다. 행은 INSERT 전용이다.

각 행의 체크섬은 **직전 행의 체크섬을 입력에 포함**해 계산된다.
행마다 독립적인 해시를 저장하던 이전 방식은 행 하나를 **통째로 지우면** 남은 행들이
전부 자기 검증을 통과해 탐지되지 않았다 — 감사 추적에서 가장 흔한 은폐 수법이
정확히 그것이다. 체인으로 엮으면 삭제·삽입·재배열이 연결 고리를 끊는다.

| 조작 | 탐지 신호 |
|------|----------|
| 행 내용 수정 | `CHECKSUM_MISMATCH` — 다시 계산한 해시가 저장값과 다르다 |
| 중간 행 삭제·삽입·순서 변경 | `BROKEN_LINK` — 다음 행의 `prev_checksum`이 이웃과 안 맞는다 |
| 마지막 행 삭제 | `HEAD_MISMATCH` — `audit_chain_head`에 보관한 머리 해시와 어긋난다 |

해시 입력에 `detail`과 `ip_address`도 포함한다. 이전 구현은 이 둘을 빼고 해시해서
"누가 무엇을 했는지"가 적힌 `detail`을 고쳐도 체크섬이 그대로였다.

검증: `GET /admin/audit-logs/verify` (ADMIN)

**한계** — 체인은 위변조를 *탐지*할 뿐 *막지는* 못한다. DB 쓰기 권한자는 행을 지운 뒤
이후 행 전부와 머리 해시를 다시 계산해 넣을 수 있다. 그것까지 막으려면 머리 해시를
주기적으로 외부(다른 권한 도메인)에 고정해야 한다.

### 7. KYC 고객확인
- 계좌별 신원 정보 등록 (원문 식별번호 비저장, 마스킹값만 보관)
- RISK_OFFICER가 `VERIFIED` 승인
- 위험 등급(`LOW`/`MEDIUM`/`HIGH`) 관리

---

## API 명세

Swagger UI: **http://localhost:8000/docs**

| 메서드 | 경로 | 설명 | 최소 권한 |
|--------|------|------|----------|
| `POST` | `/auth/token` | JWT 토큰 발급 | 없음 |
| `GET` | `/auth/me` | 내 계정 정보 | 모든 사용자 |
| `POST` | `/transactions/transfer` | 계좌 이체 (계좌 행 잠금 + 잔액 검증, `Idempotency-Key` 헤더) | STAFF |
| `GET` | `/transactions` | 거래 목록 | STAFF |
| `GET` | `/transactions/accounts/{account_id}` | 계좌 잔액 조회 | STAFF |
| `GET` | `/fds/alerts` | FDS 알림 목록 | RISK_OFFICER |
| `GET` | `/fds/alerts/{id}` | FDS 알림 상세 | RISK_OFFICER |
| `POST` | `/fds/alerts/{id}/review` | 알림 검토 (승인/기각) | RISK_OFFICER |
| `GET` | `/fds/comparison` | 룰 단독 vs 룰+IF 앙상블 성능 비교 (재측정 예정, 현재 `status: "pending"`) | RISK_OFFICER |
| `GET` | `/fds/rules` | FDS 룰 목록 | ADMIN |
| `PUT` | `/fds/rules/{id}` | FDS 룰 수정 | ADMIN |
| `GET` | `/compliance/reports` | STR 보고서 목록 | RISK_OFFICER |
| `POST` | `/compliance/reports/{id}/review` | 초안 검토 — 보고 대상(APPROVE) / 불필요(DISMISS), 사유 필수 | RISK_OFFICER |
| `POST` | `/compliance/reports/{id}/submit` | 승인된 보고서 제출 처리 | RISK_OFFICER |
| `POST` | `/kyc` | KYC 등록 | STAFF |
| `GET` | `/kyc/{account_id}` | KYC 조회 | RISK_OFFICER |
| `PUT` | `/kyc/{account_id}/verify` | KYC 승인 | RISK_OFFICER |
| `GET` | `/kyc` | KYC 전체 목록 | RISK_OFFICER |
| `POST` | `/admin/users` | 사용자 생성 | ADMIN |
| `GET` | `/admin/users` | 사용자 목록 | ADMIN |
| `PUT` | `/admin/users/{id}/deactivate` | 사용자 비활성화 | ADMIN |
| `GET` | `/admin/audit-logs` | 감사 로그 조회 | ADMIN |
| `GET` | `/admin/audit-logs/verify` | 감사 로그 해시 체인 검증 | ADMIN |
| `GET` | `/health` | 헬스체크 | 없음 |
| `GET` | `/metrics` | Prometheus 메트릭 | 없음 |

---

## 데이터 모델

| 테이블 | 설명 |
|--------|------|
| `accounts` | 계좌 원장 — 잔액 `NUMERIC(18,2)` (이체 시 실제로 증감) |
| `transactions` | 거래 내역 (risk_score, 이체 전후 잔액 포함) |
| `users` | 사용자 계정 (RBAC) |
| `audit_logs` | 불변 감사 추적 (SHA-256 해시 체인) |
| `audit_chain_head` | 감사 체인 머리 해시 — 꼬리 삭제 탐지 + 기록 직렬화 지점 |
| `fds_rules` | FDS 탐지 룰 (DB 기반 관리) |
| `fds_alerts` | FDS 이상거래 알림 |
| `fds_decisions` | 알림 검토 결정 이력 |
| `compliance_reports` | STR 보고서 (DRAFT → APPROVED/DISMISSED → SUBMITTED) |
| `idempotency_keys` | 이체 멱등성 키 — (user_id, key) 유니크, 요청 해시, 확정된 거래 |
| `kyc_records` | 고객확인 정보 |

---

## 실행 방법

### 1. 환경 변수 설정

```bash
copy .env.example .env
```

운영 배포 전 `.env`의 `JWT_SECRET_KEY`를 강력한 랜덤 값으로 교체:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. 서비스 실행

```bash
docker compose up --build
```

### 3. 접속 주소

| 서비스 | 주소 |
|--------|------|
| API (Swagger UI) | http://localhost:8000/docs |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

### 4. ML 평가 파이프라인
```bash
python scripts/load_paysim.py <PaySim CSV 경로>  # PaySim 데이터를 DB에 적재
python scripts/train_model.py                    # Isolation Forest 학습
python scripts/evaluate.py                       # 룰 vs ML vs 앙상블 평가
```

### 5. 기본 계정

| 사용자명 | 비밀번호 | 역할 |
|---------|---------|------|
| `admin` | `Admin1234!` | ADMIN |
| `risk_officer` | `Risk1234!` | RISK_OFFICER |
| `staff` | `Staff1234!` | STAFF |

> Swagger UI → 우측 상단 **Authorize** → `/auth/token`으로 로그인 후 모든 API 사용 가능

---

## 테스트 실행

```bash
# 의존성 설치
pip install pytest

# 전체 테스트 실행 (SQLite 파일 DB ./test.db 사용 — Docker 불필요)
pytest tests/ -q

# 같은 테스트를 실제 MySQL 8.0 컨테이너에서 (Docker 필요, pip install "testcontainers[mysql]")
FDS_TEST_DB=mysql pytest tests/ -q
```

현재 환경에서 실행한 결과: SQLite **111 passed, 6 skipped** (MySQL 전용 테스트는 건너뜀) / MySQL **117 passed**

> Windows + Python 3.14에서는 docker SDK가 named pipe에 연결하지 못해(`NpipeSocket` 비호환) Testcontainers가 뜨지
> 않는다. MySQL 테스트는 Python 3.13 가상환경에서 돌렸다.

| 파일 | 건수 | 범위 |
|---|---:|---|
| `test_concurrency.py` | 6 | 이체 행 잠금 — 초과 인출·금액 보존·잠금 순서 |
| `test_mysql_locking.py` | 6 | **MySQL 전용** — FOR UPDATE 제거 시 초과 인출(대조군), 정렬 없는 잠금의 데드락 재현, 데드락 재시도, 잠금 대기 초과 → 503 |
| `test_money_precision.py` | 5 | 거래 금액이 저장 후에도 정확한가 (MySQL FLOAT 잘림 회귀) |
| `test_idempotency.py` | 9 | 같은 키 동시 12건 → 출금 1건, 키 기록 끄면 12건(대조군), 유니크 제약만으로 보장, 키 재사용 409, 확정 실패 재현, 예외 시 키 롤백 |
| `test_audit_chain.py` | 12 | 감사 해시 체인 — 수정·중간 삭제·꼬리 삭제 탐지 |
| `test_calibration.py` | 19 | lift/AUC/IV 지표, 룰 신호 생성, 점수 도달 가능성(룰만으로 HIGH 도달, HIGH의 BALANCE_DRAIN 의존) |
| `test_fds.py` | 16 | FDS 룰 엔진·위험점수·알림 |
| `test_scoring_parity.py` | 4 | 운영 경로(DB 조회)와 오프라인 평가 경로의 신호·룰 점수·ML 피처 일치 |
| `test_single_source.py` | 3 | α·IF 생성·IF 정규화·룰 기본값이 한 곳에만 정의됨, ML 피처는 거래 전 값만 |
| `test_kyc.py` / `test_transactions.py` / `test_auth.py` / `test_compliance.py` | 37 | KYC, 거래(MEDIUM 미만 무알림, 잔액 비우기 → HIGH → STR 초안), 인증·RBAC, STR 검토 흐름 |

---

## 향후 계획

우선순위 순. 기능 추가보다 이미 진단한 결함을 고쳐 "진단 → 개선 → 재측정"을 완결하는 것이 먼저다.

1. **탐지 엔진** — 완료(여기서 마무리): 평가 코드가 운영 엔진을 import, 시간 분할 평가, IF 피처를 거래 전 정보로 제한,
   룰을 사기 신호로 교체(가중치는 데이터로 결정), 알림 임계값 매일 갱신. 남은 것: 원화 운영 데이터로 룰 재보정 —
   특히 HIGH가 의존하는 BALANCE_DRAIN. 모델 종류·앙상블 방식은 더 파고들지 않는다.
2. **거래 정합성** — 완료: 이체 멱등성 키, Testcontainers MySQL로 `FOR UPDATE` 동시성 실검증(데드락 재현·재시도,
   잠금 대기 초과). 다음: 복식부기 원장과 대사 배치, 부하 테스트(이체 TPS·p95, 동기 채점의 지연 기여,
   `audit_chain_head` 병목).
3. **CTR** — 현금 거래 유형과 고객 식별자 추가 후 동일인 1거래일 합산으로 구현.

드리프트 모니터링, MLflow, LOF는 기본 탐지기가 개선된 뒤로 미룬다.

## Grafana 대시보드

Grafana 로그인: `admin` / `admin`

포함 패널:
- 총 거래 건수 / 실패 건수 / FDS 알림 / 로그인 실패
- 고액거래·로그인 실패 이상징후
- STR 초안 건수
- FDS 알림 유형별 추이 (timeseries)
- 거래 위험점수 분포 (p50 / p95)
- API 응답 시간 (p95)
- 컴플라이언스 보고서 누적

---

## 기술 스택

- **Backend**: Python 3.11, FastAPI 0.103, SQLAlchemy 2.0
- **Auth**: python-jose (JWT HS256), bcrypt
- **ML**: scikit-learn (Isolation Forest), numpy, joblib
- **DB**: MySQL 8.0
- **Monitoring**: Prometheus, Grafana
- **Container**: Docker Compose
- **Test**: pytest, SQLite (인메모리)