# 금융 거래 이상징후 모니터링 대시보드

## 프로젝트 개요

`금융 거래 이상징후 모니터링 시스템`은 FastAPI 기반 가상 거래 API를 제공하고, 거래 상태와 시스템 지표를 Prometheus로 수집하여 Grafana에서 시각화하는 포트폴리오용 운영 모니터링 프로젝트입니다.

이 프로젝트는 금융권 IT/운영 직무에 맞춰 설계되었으며, SQLD 준비를 고려해 MySQL 기반 데이터 저장과 Docker Compose 기반 운영 환경을 포함합니다.

## 아키텍처 구성

- `transaction-api`: FastAPI 기반 거래 API 서버
- `mysql`: 거래 내역 저장용 MySQL 컨테이너
- `prometheus`: `/metrics` 메트릭을 스크랩하는 Prometheus 컨테이너
- `grafana`: Prometheus를 시각화하는 Grafana 컨테이너

```
transaction-api -> MySQL
transaction-api -> Prometheus -> Grafana
```

## 핵심 기능

- 거래 생성 및 성공/실패 처리
- 거래 데이터 `transactions` 테이블 저장
- `/metrics` Prometheus 메트릭 제공
- Prometheus 스크랩 설정
- Grafana 대시보드 프로비저닝
- 로그인 실패 이벤트 메트릭 수집

## 제공 API

- `POST /transactions/transfer` - 거래 요청
- `GET /transactions` - 최근 거래 목록 조회
- `GET /health` - 서비스 상태 확인
- `GET /metrics` - Prometheus 메트릭
- `POST /auth/login` - 로그인 실패 이벤트 시뮬레이션

## 모니터링 지표

- `transaction_total`: 전체 거래 수
- `transaction_failed_total`: 실패 거래 수
- `http_request_duration_seconds`: API 응답 시간
- `http_requests_total`: HTTP 요청 수
- `login_failed_total`: 로그인 실패 횟수
- `anomaly_event_total`: 이상징후 탐지 수
- `anomaly_transaction_failure_total`: 거래 실패율 이상징후 수
- `anomaly_latency_total`: 응답 지연 이상징후 수
- `anomaly_login_failure_total`: 반복 로그인 실패 이상징후 수
- `anomaly_high_value_total`: 고액 거래 이상징후 수

## 이상징후 탐지 기준

- 거래 실패율 경보: 최근 거래 중 실패 비율이 30% 이상일 때
- 응답 지연 경보: API 응답 시간이 1초 이상일 때
- 보안 이상징후: 동일 사용자의 로그인 실패가 5분 이내 3회 이상일 때
- 고액 거래 이상징후: 거래 금액이 100,000 이상일 때

## 실행 방법

1. `.env.example` 복사 후 `.env` 생성

```bash
copy .env.example .env
```

2. Docker Compose 실행

```bash
docker compose up --build
```

3. 서비스 접속

- API: http://localhost:8000
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

Grafana 기본 관리자 계정
- 사용자: `admin`
- 비밀번호: `admin`

## 검증 방법

### 1) API 기본 동작 확인

```bash
curl -X GET http://localhost:8000/health
curl -X GET http://localhost:8000/transactions
curl -X GET http://localhost:8000/metrics
```

### 2) 거래 요청 테스트

```bash
curl -X POST http://localhost:8000/transactions/transfer \
  -H "Content-Type: application/json" \
  -d '{"account_from":"A123","account_to":"B456","amount":120000,"currency":"KRW"}'
```

### 3) 로그인 실패 이상징후 테스트

```bash
for i in 1 4; do
  curl -X POST http://localhost:8000/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"user1","password":"wrong"}'
  echo
 done
```

### 4) 응답 지연 이상징후 테스트

- `/metrics` 또는 Grafana에서 `anomaly_latency_total`이 증가했는지 확인
- 실제 지연값은 `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))`로 확인

### 5) 시뮬레이터 동작 확인

Docker Compose로 실행하면 시뮬레이터가 자동으로 API 요청을 생성합니다.
- `transaction-api`에 정상 거래, 실패 거래, 고액 거래가 자동 생성됩니다.
- 동일 사용자(`simuser`)에 대한 로그인 실패가 반복 생성됩니다.
- Grafana 대시보드에서 `anomaly_event_total`, `anomaly_high_value_total`, `anomaly_login_failure_total` 값이 증가하는지 확인합니다.

### 6) 이상징후 지표 확인

- Prometheus에서 `anomaly_event_total`, `anomaly_high_value_total`, `anomaly_login_failure_total`, `anomaly_transaction_failure_total`을 조회
- Grafana 대시보드에서 이상징후 패널을 확인

## 시뮬레이터 역할

`simulator` 서비스는 `transaction-api`가 실행된 이후 자동으로 다음을 수행합니다.
- 주기적으로 `POST /transactions/transfer` 거래 요청 생성
- 정상 거래, 실패 거래, 고액 거래를 섞어 생성
- 동일 사용자에 대해 반복 로그인 실패 요청 생성
- Prometheus/Grafana에서 실시간으로 메트릭 변화를 관찰할 수 있도록 데이터 공급

## 개발 구조

- `app/`: FastAPI 애플리케이션 코드
- `Dockerfile`: API 서버 이미지 빌드
- `docker-compose.yml`: 전체 서비스 구성
- `prometheus/prometheus.yml`: Prometheus 스크랩 설정
- `grafana/`: Grafana 데이터 소스 및 대시보드 프로비저닝
- `.env.example`: 실행 환경 변수 예시

## 향후 개선 방향

- 이상징후 탐지를 위한 rule-based 로직 추가
- Grafana Alerting 또는 Alertmanager 연동
- 거래 금액 이상치, 반복 실패 계좌 탐지 규칙 구현
- Loki 또는 DB 기반 로그 분석 추가
