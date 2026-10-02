-- 거래 기록·보고서의 금액·잔액 컬럼을 FLOAT → DECIMAL(18,2), 점수·임계값을 FLOAT → DOUBLE (MySQL)
--
-- SQLAlchemy Float는 MySQL에서 단정밀도 FLOAT이고, 읽을 때 유효숫자 6자리로 돌아온다.
-- 1,234,567원 → 1,234,570원, 12,345.67원 → 12,345.70원으로 기록되고 있었다.
-- (원장 accounts.balance는 처음부터 DECIMAL(18,2)라 잔액 자체는 정확했다.)
--
-- 주의: 이미 FLOAT로 저장된 값은 이 변환으로 복구되지 않는다 — 잘린 값이 그대로 DECIMAL로 옮겨진다.
--   - 거래 기록 안에서는 복구할 수 없다. 이체 전후 잔액 컬럼(balance_*)도 같은 FLOAT라 함께 잘렸다.
--   - 감사 로그로는 원 단위까지 복구할 수 있다. audit_logs.detail에 "amount=1,234,567" 형식(원 단위 정수,
--     :,.0f)으로 요청 금액이 남아 있다. 원화 금액은 정수이므로 KRW 거래는 정확히 복구된다(원 미만이 있던
--     외화 거래는 원 미만을 잃는다). 복구 전에 GET /admin/audit-logs/verify로 체인이 온전한지 먼저 확인한다.
--   - 계좌 잔액(accounts.balance)은 처음부터 DECIMAL(18,2)라 영향이 없다.

ALTER TABLE transactions
  MODIFY amount              DECIMAL(18,2) NOT NULL,
  MODIFY balance_orig_before DECIMAL(18,2) NULL,
  MODIFY balance_orig_after  DECIMAL(18,2) NULL,
  MODIFY balance_dest_before DECIMAL(18,2) NULL,
  MODIFY balance_dest_after  DECIMAL(18,2) NULL,
  MODIFY risk_score          DOUBLE NOT NULL,
  MODIFY ml_anomaly_score    DOUBLE NULL,
  MODIFY ensemble_score      DOUBLE NULL;

ALTER TABLE compliance_reports
  MODIFY amount DECIMAL(18,2) NOT NULL;

ALTER TABLE fds_rules
  MODIFY threshold DOUBLE NOT NULL,
  MODIFY weight    DOUBLE NOT NULL;

ALTER TABLE fds_alerts
  MODIFY risk_score DOUBLE NOT NULL;
