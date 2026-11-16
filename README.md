# InfraPulse — 금융 이상거래 탐지 시스템(FDS)

계좌 이체 원장 위에서 이상거래를 탐지·검토·보고하는 **FDS(이상금융거래탐지시스템)** 입니다.  
한국 금융 규정을 기준으로 이체 정합성, 탐지 룰, 담당자 검토, STR 초안, 감사 추적까지 구현했습니다.

**핵심 결과**
- **이체 정합성을 실제 MySQL에서 검증했다** — `FOR UPDATE` 행 잠금(빼면 초과 인출 재현), 데드락 재현·재시도, 멱등성 키
  (같은 키 동시 12건 → 출금 1건), 복식부기 대사. 그 과정에서 SQLite 테스트가 놓친 **거래 금액 잘림 버그**(MySQL FLOAT,
  1,234,567원 → 1,234,570원)를 찾아 고쳤다. CI가 SQLite·MySQL 두 구성으로 매번 돈다.
- **탐지 룰을 데이터로 다시 만들었다** — 무작위 수준(평가 구간 AUC 0.545)이던 룰을 거래 전 사기 신호로 교체해, 시간 분할 평가에서
  AUC 0.810, 같은 알림 예산 재현율이 무작위의 33배(합성 데이터 특성에 기대는 룰을 뺀 보수적 수치).
- **평가 코드가 운영 엔진을 그대로 쓴다** — 따로 구현한 기준선으로 낸 비교표("FPR ▼50%")는 철회했고, 운영·평가 경로가
  같은 값을 내는지 동등성 테스트로 고정했다(운영 코드의 동률 처리 버그 2건을 찾음).
- **고위험 이체는 실행하지 않고 보류한다** — 룰 점수 HIGH면 금액을 보류 계정에 묶고 담당자 승인 시 입금, 취소·시간 초과 시
  반환(통신사기피해환급법 제2조의5 임시조치의 취지를 거래 단위로). 승인·취소가 동시에 와도 한 번만 반영된다. 대가도 쟀다 —
  평가 구간에서 **보류 100건 중 61건이 정상 거래**이고, 그 거의 전부가 정확히 70점인 룰 조합 하나에서 나온다.
- **부하를 진단하고 고친 뒤 다시 쟀다**(노트북, 단일 프로세스): 채점 쿼리 3개가 인덱스 없이 전체 스캔이었다. 거래 100만 행에서
  인덱스 전후 처리량 11.4 → 83.6 req/s, p95 2.5초 → 355ms(3회 반복). 남은 최대 비용은 감사 해시 체인(끄면 +28%)이다.

---

## 규정 근거

| 기능 | 근거 법령 |
|------|-----------|
| 이상거래 탐지·임시조치(이체 지연·일시 정지) — 취지를 **거래 단위 보류**로 구현. 계좌 단위 조치·이용자 통지·본인확인은 미구현, 2절 참고 | 전기통신금융사기 피해 방지 및 피해자산 환급에 관한 특별법(통신사기피해환급법) 제2조의5 |
| FDS 운영 기준(모범사례, 법규 아님) | 금융감독원·금융보안원 「이상금융거래탐지시스템(FDS) 운영 가이드라인」(2023년 10월 발표) |
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
              │ (12개 테이블)│    │  (메트릭 수집)│
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
## 핵심 기능

### 1. JWT 기반 RBAC 인증
JWT(HS256) 토큰 발급 및 역할 기반 접근 제어.

| 역할 | 권한 |
|------|------|
| `STAFF` | 거래 생성·조회, KYC 등록 |
| `RISK_OFFICER` | FDS 알림 검토, 컴플라이언스 보고서 제출, KYC 승인 |
| `ADMIN` | 전체 권한 + FDS 룰 관리 + 감사 로그 조회 + 사용자 관리 |

### 2. 거래 정합성 — 행 잠금 · 멱등성 · 복식부기 · 임시조치

상세(설계 근거, 검증 표, 부하 측정): [docs/transaction-integrity.md](docs/transaction-integrity.md)

| 문제 | 해법 | 검증 (SQLite·MySQL) |
|---|---|---|
| 동시 이체의 초과 인출·lost update | 계좌 행 `SELECT ... FOR UPDATE`, 계좌번호 오름차순 잠금, 원장 `NUMERIC(18,2)` | 12건 동시 → 성공은 잔액분만. 대조군: 잠금을 빼면 초과 인출, 정렬을 빼면 MySQL 데드락(1213) |
| 재전송으로 인한 이중 출금 | `Idempotency-Key` + `(user_id, key)` 유니크 제약, 잔액 변경과 같은 커밋 | 같은 키 12건 동시 → 출금 1건. 대조군: 키 기록을 끄면 12건 |
| 잠금 충돌 | 데드락·잠금 대기 초과는 지수 백오프 3회 재시도 후 503 | 실제 데드락 뒤 재시도로 두 건 모두 성공, 금액 보존 |
| 잔액 컬럼의 근거 | 추가 전용 복식부기 `ledger_entries`, 대사 3종(`scripts/reconcile.py`) | 잔액 조작·분개 한쪽 삭제·보류 이중 해제를 대사가 잡아냄 |
| HIGH 이체 | 보류 계정에 묶고 승인 시 입금, 취소·만료 시 반환. `WHERE status='held'` 조건 UPDATE로 한 번만 해제 | 승인·취소 동시 12건 → 1건 반영. 대조군: 읽고 나서 쓰면 MySQL에서 두 번 해제 |

- **MySQL에서 처음 드러난 버그**: 금액 컬럼이 단정밀도 FLOAT라 1,234,567원이 1,234,570원으로 기록됐다. SQLite 테스트는 모두
  통과했었다. 금액은 `NUMERIC(18,2)`, 요청 금액은 `Decimal`로 바꿨다.
- **보류의 오탐 비용**: 평가 구간에서 보류 100건 중 61건이 정상 거래이고, 그 거의 전부가 정확히 70점인 룰 조합 하나(정밀도 25.6%)에서
  나온다. 평가 구간을 보고 임계값을 바꾸지는 않았다. 고객이 본인확인으로 직접 푸는 경로가 필요하다는 근거다.
- **조문과의 차이**: 통신사기피해환급법 제2조의5의 임시조치는 계좌 단위이고 통지·본인확인·해제를 요구한다. 여기서는 그 취지를
  거래 단위 보류로 구현했고, 계좌 단위 조치·통지·본인확인은 하지 않았다. 24시간 만료(`hold-expirer` 작업자)는 이 시스템의 선택이다.
- **부하**(100만 행, 3회 반복): 채점 인덱스 전후 11.4 → 83.6 req/s, p95 2.5초 → 355ms. 보류 경로의 추가 비용은 측정 편차 안이다.
  남은 최대 비용은 감사 해시 체인(끄면 처리량 +28%)이다.

### 3. FDS 룰 엔진과 탐지 평가

상세(룰 근거, 이전 룰 진단, 평가 설계, 알림 비율): [docs/detection.md](docs/detection.md)

임계값·가중치는 DB에서 관리해 재시작 없이 바꾼다. 모든 룰은 **이체 실행 전에 알 수 있는 정보**만 쓴다.

| 룰 유형 | 임계값 | 점수 | 근거 (PaySim 1~9일 lift) |
|---------|--------|-----:|------|
| `HIGH_VALUE` (고액, 금액 상위 5%) | 827,513 | +20 | 7.3 |
| `HIGH_VALUE_TOP` (초고액, 상위 1% — 위에 추가) | 1,775,460 | +10 | 21.3 |
| `BALANCE_DRAIN` (거래 전 잔액의 90~100% 인출, 잔액 > 0) | 0.9 | +45 | 103.5 |
| `DEST_EMPTY` (수취 계좌 거래 전 잔액 0) | — | +15 | 4.8 |
| `NEW_RECIPIENT` (수취 계좌가 직전 24시간 입금 없음) | — | +10 | 2.4 |
| `VELOCITY` (동일 송금 계좌 10분 내 5건 이상) | 5건 | +10 | PaySim에서 측정 불가 — 기존 판단치 유지 |
| `FAILURE_RATE` / `LOGIN_FAILURE` / `LATENCY` | — | 0 | 시스템 모니터링 신호 — 거래 점수에 기여하지 않음 |

- **가중치 = 10·ln(lift)**(PaySim 1~9일). 점수는 로그 오즈 척도라 70점은 lift 약 1,100배를 뜻하지만, 정확히 70점인 조합
  (`BALANCE_DRAIN + DEST_EMPTY + NEW_RECIPIENT`)은 세 룰이 한 모양을 겹쳐 세서 실측 lift가 약 170배에 그친다.
- **주의 — PaySim 기준이다.** BALANCE_DRAIN은 합성 데이터 특성(사기는 잔액을 정확히 비운다)에 기댄다. 원화 운영 데이터로
  재보정하기 전에는 이 가중치를 그대로 쓰면 안 된다.
- **위험 등급**: LOW(0~39) 기록 / MEDIUM(40~69) FDS 알림 / HIGH(70~) **이체 보류** + 알림 + STR 초안.

**평가** — 평가 코드는 운영 엔진의 점수 함수를 그대로 import한다(동등성 테스트로 고정). 시간 분할: 룰 결정 1~9일, 검증 10~12일,
평가 13~17일은 마지막에 한 번만 열었다. 평가 13~17일(886,056건, 사기 0.148%), IF는 시드 5개 평균 ± 표준편차:

| 방법 | ROC-AUC | PR-AUC | 재현율@0.5% |
|---|---:|---:|---:|
| 이전 룰 (참고) | 0.545 | 0.0016 | — |
| 룰 | 0.997 | 0.526 | 0.803 |
| 룰 (BALANCE_DRAIN 제외) | 0.810 | 0.015 | 0.164 |
| Isolation Forest | 0.906 ± 0.003 | 0.145 ± 0.029 | 0.341 ± 0.012 |
| 앙상블 | 0.992 ± 0.000 | 0.565 ± 0.013 | 0.636 ± 0.021 |
| 앙상블 (BALANCE_DRAIN 제외) | 0.918 ± 0.002 | 0.291 ± 0.007 | 0.353 ± 0.002 |

- **대표 수치는 BALANCE_DRAIN 제외 기준이다**: 룰 ROC-AUC 0.810, 같은 예산 재현율 0.164(무작위의 33배). 이전 룰은 0.545였다.
- 앙상블은 순위 전체(PR-AUC)에서는 IF보다 낫지만, 예산 0.5% 지점의 재현율 차이(0.353 vs 0.341)는 시드 편차 안이라 구분할 수 없다.
  ML 앙상블은 기본으로 꺼져 있다(PaySim은 USD라 원화 거래 채점에 의미가 없다).
- 이전 ULB 비교표("FPR ▼50%")는 철회했다. 평가 스크립트가 운영 엔진 대신 따로 구현한 기준선을 썼다.

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
  70점(HIGH)은 룰만으로 도달할 수 있지만, 도달하는 모든 조합에 BALANCE_DRAIN이 들어 있다([docs/detection.md](docs/detection.md) 3절 —
  `test_rule_only_config_can_reach_high`, `test_high_requires_balance_drain`).
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
| `POST` | `/transactions/transfer` | 계좌 이체 (계좌 행 잠금 + 잔액 검증, `Idempotency-Key` 헤더). HIGH면 202 `held` | STAFF |
| `GET` | `/transactions` | 거래 목록 | STAFF |
| `GET` | `/transactions/accounts/{account_id}` | 계좌 잔액 조회 (가용 잔액 + 보류 금액 `held_amount`) | STAFF |
| `GET` | `/transactions/held` | 보류 중인 이체 목록 | RISK_OFFICER |
| `POST` | `/transactions/{id}/hold-decision` | 보류 이체 판단 — 승인(입금) / 취소(반환), 사유 필수 | RISK_OFFICER |
| `GET` | `/fds/alerts` | FDS 알림 목록 | RISK_OFFICER |
| `GET` | `/fds/alerts/{id}` | FDS 알림 상세 | RISK_OFFICER |
| `POST` | `/fds/alerts/{id}/review` | 알림 검토 (승인/기각) | RISK_OFFICER |
| `GET` | `/fds/stats` | FDS 운영 통계 (탐지율, 거래 실패율 등) | RISK_OFFICER |
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
| `transactions` | 거래 내역 (risk_score, 이체 전후 잔액 포함). 상태 `success` / `failed` / `held` / `cancelled` |
| `users` | 사용자 계정 (RBAC) |
| `audit_logs` | 불변 감사 추적 (SHA-256 해시 체인) |
| `audit_chain_head` | 감사 체인 머리 해시 — 꼬리 삭제 탐지 + 기록 직렬화 지점 |
| `fds_rules` | FDS 탐지 룰 (DB 기반 관리) |
| `fds_alerts` | FDS 이상거래 알림 |
| `fds_decisions` | 알림 검토 결정 이력 |
| `compliance_reports` | STR 보고서 (DRAFT → APPROVED/DISMISSED → SUBMITTED) |
| `ledger_entries` | 복식부기 분개 — 추가 전용, 이체·개설·보류·해제마다 합 0인 두 행 |
| `idempotency_keys` | 이체 멱등성 키 — (user_id, key) 유니크, 요청 해시, 확정된 거래 |
| `kyc_records` | 고객확인 정보 |

---

## 실행 방법

### 1. 환경 변수 설정

```bash
cp .env.example .env        # Windows cmd: copy .env.example .env
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
# 의존성 설치 (CI와 같은 구성)
pip install -r app/requirements.txt
pip install pytest httpx "testcontainers[mysql]"

# 전체 테스트 실행 (SQLite 파일 DB ./test.db 사용 — Docker 불필요)
pytest tests/ -q

# 같은 테스트를 실제 MySQL 8.0 컨테이너에서 (Docker 필요)
FDS_TEST_DB=mysql pytest tests/ -q
```

현재 환경에서 실행한 결과: SQLite **131 passed, 7 skipped** (MySQL 전용 테스트는 건너뜀) / MySQL **138 passed**
(CI에서도 두 구성을 모두 돌리고, `pip-audit`으로 의존성의 알려진 취약점을 검사한다 — `.github/workflows/tests.yml`)

> Windows + Python 3.14에서는 docker SDK가 named pipe에 연결하지 못해(`NpipeSocket` 비호환) Testcontainers가 뜨지
> 않는다. MySQL 테스트는 Python 3.13 가상환경에서 돌렸다.

| 파일 | 건수 | 범위 |
|---|---:|---|
| `test_concurrency.py` | 6 | 이체 행 잠금 — 초과 인출·금액 보존·잠금 순서 |
| `test_mysql_locking.py` | 6 | **MySQL 전용** — FOR UPDATE 제거 시 초과 인출(대조군), 정렬 없는 잠금의 데드락 재현, 데드락 재시도, 잠금 대기 초과 → 503 |
| `test_holds.py` | 11 | 임시조치 — 보류·승인·취소·만료(만료 작업자 포함), 보류 중 동시 이체, 승인·취소 동시 경합, 상태 확인 후 쓰기 대조군(MySQL), 멱등성 |
| `test_reconciliation.py` | 7 | 이체당 차변·대변 2행, 동시 이체 후 대사 일치, 잔액 조작·분개 다리 삭제를 대사가 잡아냄 |
| `test_money_precision.py` | 7 | 거래 금액이 저장 후에도 정확한가 (MySQL FLOAT 잘림 회귀), 소수 셋째 자리 거부, 요청→기록→감사 로그 금액 일치 |
| `test_idempotency.py` | 9 | 같은 키 동시 12건 → 출금 1건, 키 기록 끄면 12건(대조군), 유니크 제약만으로 보장, 키 재사용 409, 확정 실패 재현, 예외 시 키 롤백 |
| `test_audit_chain.py` | 12 | 감사 해시 체인 — 수정·중간 삭제·꼬리 삭제 탐지 |
| `test_calibration.py` | 19 | lift/AUC/IV 지표, 룰 신호 생성, 점수 도달 가능성(룰만으로 HIGH 도달, HIGH의 BALANCE_DRAIN 의존) |
| `test_fds.py` | 17 | FDS 룰 엔진·위험점수·알림, 이체 경로에서 점수에 안 쓰는 실패율 조회를 하지 않음 |
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
   잠금 대기 초과), 복식부기 엔트리와 대사, 고위험 이체 보류(임시조치)와 만료 작업자, 채점 인덱스와 100만 행 재측정.
   다음 대상: 다중 프로세스 검증, 장애 주입(커밋 직전·직후 강제 종료) 테스트, 감사 체인 단일 행 직렬화와 머리 해시 외부 고정,
   보류의 본인확인 해제 경로와 정확히 70점인 조합의 보류 여부(룰 결정 구간으로 다시 판단).
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

- **Backend**: Python 3.11 (Docker 이미지·CI 기준. 로컬에서는 3.13·3.14로도 돌렸다), FastAPI 0.111 이상(requirements 하한), SQLAlchemy 2.0
- **Auth**: PyJWT (JWT HS256), bcrypt
- **ML**: scikit-learn (Isolation Forest), numpy, joblib
- **DB**: MySQL 8.0
- **Monitoring**: Prometheus, Grafana
- **Container**: Docker Compose
- **의존성 점검**: `pip-audit`을 CI에서 돌린다. 처음 돌렸을 때 6개 패키지에서 알려진 취약점이 나왔다. python-jose는 수정 버전이
  없는 권고가 있고 ecdsa까지 끌고 와서 PyJWT로 교체했고, cryptography·python-multipart·requests·python-dotenv는 수정 버전으로 올렸다.
- **Test**: pytest, SQLite(파일 DB `./test.db`), Testcontainers MySQL 8.0, GitHub Actions(두 DB 구성 + pip-audit)