-- 복식부기 엔트리 테이블 (MySQL) — app/models.py LedgerEntry
--
-- 추가 전용. 이체마다 출금 −금액·입금 +금액 두 행, 계좌 개설마다 시스템 자본 계정 −·새 계좌 + 두 행.
-- 대사(scripts/reconcile.py)는 (1) 분개마다 합 0, (2) 계좌 잔액 = 엔트리 누적을 검사한다.

CREATE TABLE IF NOT EXISTS ledger_entries (
  id             INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
  journal_id     VARCHAR(64)   NOT NULL,
  account_id     VARCHAR(64)   NOT NULL,
  amount         DECIMAL(18,2) NOT NULL,
  currency       VARCHAR(8)    NOT NULL DEFAULT 'KRW',
  transaction_id INT           NULL,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_ledger_entries_journal_id (journal_id),
  INDEX ix_ledger_entries_account_id (account_id),
  CONSTRAINT fk_ledger_entries_transaction FOREIGN KEY (transaction_id) REFERENCES transactions (id)
);

-- 이 테이블이 생기기 전의 계좌에는 개설 분개가 없어, 그대로 두면 대사가 모든 계좌를 불일치로 보고한다.
-- 도입 시점의 잔액을 기준선 분개로 한 번 기록한다. 이 기준선 이전의 이력은 엔트리로 검증되지 않는다 —
-- 그 구간은 감사 로그와 거래 기록으로만 확인할 수 있다.
INSERT INTO ledger_entries (journal_id, account_id, amount, currency)
SELECT CONCAT('BASELINE-', account_id), 'SYS-OPENING-EQUITY', -balance, currency FROM accounts;
INSERT INTO ledger_entries (journal_id, account_id, amount, currency)
SELECT CONCAT('BASELINE-', account_id), account_id, balance, currency FROM accounts;
