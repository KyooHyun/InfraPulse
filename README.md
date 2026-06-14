# FDS — 금융 이상거래 탐지 시스템

금융권 포트폴리오용 **FDS(이상금융거래탐지시스템)** 구현체입니다.  
한국 금융 규정을 기반으로 이상거래 탐지·검토·보고 전 과정을 구현했습니다.

---

## 규정 근거

| 기능 | 근거 법령 |
|------|-----------|
| FDS 의무 운영 | 전자금융감독규정 제37조의2 |
| STR (의심거래보고) | 특정금융정보법 제4조 |
| CTR (고액현금거래보고, 1천만원↑) | 특정금융정보법 제4조의2 |
| KYC (고객확인제도) | 특정금융정보법 제5조의2 |
| 감사 추적 보존 | 전자금융거래법 제22조 |

---

## 시스템 아키텍처

```
                  ┌─────────────────────────────────────┐
                  │         transaction-api              │
                  │  FastAPI  │  FDS Engine  │  Reports  │
                  │  JWT/RBAC │  Rule + IF   │  STR/CTR  │
                  └────┬──────────────┬──────────────────┘
                       │              │
              ┌────────▼──┐    ┌──────▼──────┐
              │   MySQL   │    │  Prometheus  │
              │  (8개 테이블)│    │  (메트릭 수집)│
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
- `HIGH` (70~100점): FDS 알림 생성 + STR(의심거래보고서) 자동 생성

### 3. 비지도학습(Isolation Forest) 앙상블 + 성능 검증

HIGH_VALUE 룰 후보군 안에 거짓경보가 많은 룰 기반의 한계를 보완하기 위해, Isolation Forest(비지도학습)로 거래별 이상 점수를 추가 산출하고 룰 점수와 가중 앙상블한다.

```
hybrid_score = α × rule_score + (1-α) × if_score
```

라벨 있는 공개 데이터셋(ULB Credit Card Fraud, n=284,807, fraud=492)으로 룰 단독 vs 앙상블 오프라인 비교 결과:

| 지표 | 룰 단독 | 룰+IF 앙상블 | 변화 |
|------|---------|-------------|------|
| FPR | 4.99% | 2.50% | **▼50.0%** |
| Recall | 8.74% | 8.33% | ▼0.41%p |
| 거짓경보 | 14,199건 | 7,096건 | **7,103건 감소** |

탐지율 손실을 최소화(-0.41%p)하면서 운영팀의 거짓경보 검토 부담을 절반으로 감소.  
단, 사기 표본이 작아 recall 변화는 참고치.

초기 PaySim 데이터셋 시도 시 고액 거래가 사기·정상 무관하게 이상치로 분류되는 한계를 진단하고, PCA 피처 기반 데이터셋(V1-V28)으로 교체. 평가 상세: [`evaluation/README.md`](evaluation/README.md), 결과 조회: `GET /fds/comparison`

### 4. FDS 이상거래 검토 워크플로우
```
이상 감지 → DETECTED → (담당자 검토) → APPROVED(정상) / REJECTED(이상거래 확정)
```

### 5. 컴플라이언스 자동 보고
- **CTR**: 1천만원 이상 거래 발생 즉시 자동 생성
- **STR**: 위험점수 70점 이상 거래에 자동 생성
- 각 보고서에 고유 번호 부여 (`CTR-20260605-A1B2C3D4`)
- RISK_OFFICER가 SUBMITTED 처리 (실제 환경에서는 KoFIU API 연동)

### 6. 불변 감사 추적 (Audit Trail)
모든 중요 이벤트를 `audit_logs` 테이블에 기록.  
각 행에 **SHA-256 체크섬**을 포함해 위변조 여부 검증 가능.  
행은 INSERT 전용이며 수정·삭제하지 않는다.

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
| `POST` | `/transactions/transfer` | 계좌 이체 | STAFF |
| `GET` | `/transactions` | 거래 목록 | STAFF |
| `GET` | `/fds/alerts` | FDS 알림 목록 | RISK_OFFICER |
| `GET` | `/fds/alerts/{id}` | FDS 알림 상세 | RISK_OFFICER |
| `POST` | `/fds/alerts/{id}/review` | 알림 검토 (승인/기각) | RISK_OFFICER |
| `GET` | `/fds/comparison` | 룰 단독 vs 룰+IF 앙상블 성능 비교 | RISK_OFFICER |
| `GET` | `/fds/rules` | FDS 룰 목록 | ADMIN |
| `PUT` | `/fds/rules/{id}` | FDS 룰 수정 | ADMIN |
| `GET` | `/compliance/reports` | STR/CTR 보고서 목록 | RISK_OFFICER |
| `POST` | `/compliance/reports/{id}/submit` | 보고서 제출 처리 | RISK_OFFICER |
| `POST` | `/kyc` | KYC 등록 | STAFF |
| `GET` | `/kyc/{account_id}` | KYC 조회 | RISK_OFFICER |
| `PUT` | `/kyc/{account_id}/verify` | KYC 승인 | RISK_OFFICER |
| `GET` | `/kyc` | KYC 전체 목록 | RISK_OFFICER |
| `POST` | `/admin/users` | 사용자 생성 | ADMIN |
| `GET` | `/admin/users` | 사용자 목록 | ADMIN |
| `PUT` | `/admin/users/{id}/deactivate` | 사용자 비활성화 | ADMIN |
| `GET` | `/admin/audit-logs` | 감사 로그 조회 | ADMIN |
| `GET` | `/health` | 헬스체크 | 없음 |
| `GET` | `/metrics` | Prometheus 메트릭 | 없음 |

---

## 데이터 모델

| 테이블 | 설명 |
|--------|------|
| `transactions` | 거래 내역 (risk_score 포함) |
| `users` | 사용자 계정 (RBAC) |
| `audit_logs` | 불변 감사 추적 (SHA-256 체크섬) |
| `fds_rules` | FDS 탐지 룰 (DB 기반 관리) |
| `fds_alerts` | FDS 이상거래 알림 |
| `fds_decisions` | 알림 검토 결정 이력 |
| `compliance_reports` | STR/CTR 보고서 |
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

### 4. 기본 계정

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

# 전체 테스트 실행 (SQLite 인메모리 DB 사용 — Docker 불필요)
pytest tests/ -v
```

테스트 범위: 인증/RBAC, 거래 생성, FDS 알림 검토, 컴플라이언스 보고, KYC — 총 **30개 이상** 테스트 케이스

---

## Grafana 대시보드

Grafana 로그인: `admin` / `admin`

포함 패널:
- 총 거래 건수 / 실패 건수 / FDS 알림 / 로그인 실패
- 고액거래·로그인 실패 이상징후
- STR / CTR 보고서 건수
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