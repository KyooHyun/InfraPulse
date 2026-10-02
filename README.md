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
              │ (10개 테이블)│    │  (메트릭 수집)│
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
- `scripts/load_paysim.py`로 CSV를 DB에 적재하고, PaySim의 `is_fraud` 라벨을 그대로 보존합니다.
- `scripts/train_model.py`는 Isolation Forest를 비지도 학습으로 학습하며, 레이블은 평가용으로만 사용합니다.
- `scripts/evaluate.py`는 룰 기반 베이스라인, Isolation Forest, 그리고 룰+ML 앙상블을 비교해 precision / recall / FPR 트레이드오프를 명시합니다.
- PaySim 평가 시에는 CSV에 포함된 `TRANSFER`/`CASH_OUT` 거래와 PaySim에 존재하는 룰 피처만 사용합니다. 로그인 실패, 거래 실패율, 응답 지연과 같은 항목은 PaySim 원본 데이터에 직접 포함되지 않아 별도 평가 대상에서 제외됩니다.
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

### 2. DB 기반 FDS 룰 엔진
임계값·가중치를 DB에서 관리하여 서비스 재시작 없이 변경 가능.

| 룰 유형 | 기본 임계값 | 위험점수 기여 |
|---------|------------|--------------|
| `HIGH_VALUE` (고액거래) | 100,000원↑ | +30점 |
| `FAILURE_RATE` (거래 실패율) | 30% 이상 | +25점 |
| `LOGIN_FAILURE` (로그인 반복 실패) | 5분 내 3회 | +20점 |
| `LATENCY` (응답 지연) | 1초 이상 | +15점 |
| `VELOCITY` (고빈도 거래) | 단기 5회 이상 | +10점 |

**위험 등급:**
- `LOW` (0~39점): 기록 및 모니터링
- `MEDIUM` (40~69점): FDS 알림 생성, 담당자 검토 대기
- `HIGH` (70~100점): FDS 알림 생성 + STR(의심거래보고서) 초안 생성 → 담당자 검토

**임계값의 근거와 알려진 결함** — 위 숫자들은 금융 상식으로 정한 판단치였다.
`calibration/`에서 lift 측정과 IV 구간화로 근거를 확인했고(PaySim 2,770,409건),
그 전에 데이터 없이 산수만으로 확인되는 것부터 기록해 둔다
(`python -m calibration.reachability`).

- `LOGIN_FAILURE`(20점)와 `LATENCY`(15점)는 인증 경로·미들웨어에서 처리되는 시스템
  신호라 **거래 위험점수에 기여하지 않는다.** 룰 5개 100점처럼 보이지만 거래 한 건이
  받을 수 있는 최대 점수는 `30+25+10 = 65점`이다.
- 따라서 **룰만 쓰는 구성에서는 HIGH(70점)에 도달할 수 없다.** ML 앙상블을 켜면
  상한이 82.5점이 되어 도달한다. 같은 임계값이 두 구성에서 다른 의미를 갖는다.
- `HIGH_VALUE`가 발화하지 않으면 나머지를 다 합쳐도 35점이라 MEDIUM(40점)에 못 미친다.
  소액으로 나눠 보내는 수법이 구조적으로 검토 대기열에서 빠진다.

데이터로 확인한 것(PaySim 277만 건, TRANSFER/CASH_OUT):

- **HIGH_VALUE 10만원은 임계값 곡선의 맨 바닥에 있다.** 전체 거래의 **69.8%** 에
  발화하고 lift는 **1.14** — lift 1.0이 "아무 정보 없음"이므로 사실상 무정보 신호다.
  곡선 위쪽(상위 0.5% 지점)에서는 lift가 20.89까지 오른다.
- **MEDIUM/HIGH 등급에 해당하는 거래가 277만 건 중 0건이다.** 위 산수가 예측한
  그대로다. 룰 점수 AUC는 **0.547** — 무작위(0.5)와 거의 같다.
- 비용비를 1~200으로 흔들어도 최적 임계값이 움직이지 않고, IV 기반 경계 재설정은
  아예 해를 찾지 못한다. 점수가 가질 수 있는 값이 {0, 30} 둘뿐이기 때문이다.

측정하지 못한 것도 같이 적는다 — PaySim에는 거래 성공/실패 상태가 없어
**FAILURE_RATE 30%는 측정 불가**이고, 송금계좌 29만 개 중 2회 이상 등장하는 것이
20개뿐이라 **VELOCITY 5건/10분도 측정 불가**다. 그리고 PaySim의 금액 단위는 원화가
아니므로, 위 곡선은 "현행 값이 분포의 어디에 있는가"는 말해주지만
**"원화 10만원이 옳은가"에는 답하지 않는다.**

임계값·가중치는 이번에도 바꾸지 않았다. 확인된 것은 "현행 값이 곡선 맨 아래에 있다"
까지이고, 어디로 옮길지는 탐지 누락 비용과 검토 공수의 비율이 정한다 — 이 저장소에
없는 숫자다. 상세: [`calibration/README.md`](calibration/README.md)

### 3. 비지도학습(Isolation Forest) 앙상블 + 성능 검증

HIGH_VALUE 룰 후보군 안에 거짓경보가 많은 룰 기반의 한계를 보완하기 위해, Isolation Forest(비지도학습)로 거래별 이상 점수를 추가 산출하고 룰 점수와 가중 앙상블한다.

```
hybrid_score = α × rule_score + (1-α) × if_score
```

**성능 비교: 재측정 예정.** 이전에 ULB Credit Card 데이터셋으로 "FPR ▼50%"라고 적었던
비교표는 철회했다. 평가 스크립트가 실제 엔진을 호출하지 않고 `Amount >= p95 → 45점` 룰
하나를 따로 구현해 기준선으로 썼고(실제 HIGH_VALUE는 30점), α도 스크립트마다 달랐다.
또 FPR 감소는 임계값만 올려도 얻을 수 있어서, 같은 알림 건수에서의 recall이나 PR-AUC로
비교해야 앙상블이 낫다고 말할 수 있다. 실제 엔진의 점수 함수로 PaySim에서 다시 측정할
때까지 `GET /fds/comparison`은 `{"status": "pending"}`을 반환한다. 상세: [`evaluation/README.md`](evaluation/README.md)

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
  점수로는 초안만 만들고, 제출은 검토에서 APPROVED된 건만 할 수 있다. 검토자와 판단 사유는
  해시 체인 감사 로그에 남는다(`REVIEW_STR_APPROVE` / `REVIEW_STR_DISMISS`).
  70점은 ML 앙상블이 켜진 구성에서만 도달할 수 있다(2절 참고).
- **CTR — 미구현**: CTR 대상은 **현금** 입출금이고, 기준은 **동일인 1거래일 합산** 1천만원
  이상이다. 이 시스템에는 계좌이체만 있고 계좌를 고객 단위로 묶는 식별자도 없다. 예전에는
  이체 한 건이 1천만원 이상이면 CTR을 만들었는데, 제도와 다른 동작이라 제거했다. 현금 거래
  유형과 고객 식별자(KYC ↔ 계좌)를 추가할 때 구현한다.
- 각 보고서에 고유 번호 부여 (`STR-20260605-A1B2C3D4`). 실제 환경에서는 제출이 KoFIU API 연동으로 대체된다.
- 상태값 변경(PENDING → DRAFT) 이전에 만든 DB가 있다면
  [`scripts/migrations/2026-10-03_str_draft_status.sql`](scripts/migrations/2026-10-03_str_draft_status.sql)을
  한 번 실행한다. 기존 PENDING STR은 DRAFT로 되돌리고, 이체로 생성됐던 CTR은 지우지 않고 DISMISSED로 종결한다.

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
| `POST` | `/transactions/transfer` | 계좌 이체 (계좌 행 잠금 + 잔액 검증) | STAFF |
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
```

현재 환경에서 실행한 결과: **86 passed**

| 파일 | 건수 | 범위 |
|---|---:|---|
| `test_concurrency.py` | 6 | 이체 행 잠금 — 초과 인출·금액 보존·잠금 순서 |
| `test_audit_chain.py` | 12 | 감사 해시 체인 — 수정·중간 삭제·꼬리 삭제 탐지 |
| `test_calibration.py` | 18 | lift/AUC/IV 지표, 룰 신호 생성, 점수 도달 가능성 |
| `test_fds.py` | 16 | FDS 룰 엔진·위험점수·알림 |
| `test_kyc.py` / `test_transactions.py` / `test_auth.py` / `test_compliance.py` | 34 | KYC, 거래, 인증·RBAC, STR 검토 흐름·이체 CTR 미생성 |

---

## 향후 계획

우선순위 순. 기능 추가보다 이미 진단한 결함을 고쳐 "진단 → 개선 → 재측정"을 완결하는 것이 먼저다.

1. **탐지 엔진 재측정** — 평가 코드가 실제 엔진의 점수 함수를 import하도록 바꾸고(α는 설정 한 곳),
   사기 신호 룰(거래 전 잔액 대비 인출 비율 등)을 PaySim에서 시간 분할로 측정한다.
   알림 예산 기반 임계값, 동일 알림 건수 recall·PR-AUC 비교.
2. **거래 정합성** — 이체 멱등성 키, Testcontainers MySQL로 `FOR UPDATE` 동시성 실검증
   (데드락 재시도·락 타임아웃 포함), 복식부기 원장과 대사 배치, 부하 테스트.
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