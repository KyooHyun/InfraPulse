-- 이체 멱등성 키 테이블 (MySQL)
--
-- 같은 이체 요청이 재전송돼도 출금이 한 번만 일어나게 한다. 보장의 근거는 (user_id, key) 유니크
-- 제약이다 — 이체 경로의 사전 조회는 MySQL REPEATABLE READ의 스냅샷 때문에 방금 커밋된 키를 못 볼 수
-- 있으므로, 동시 요청은 이 제약 위반으로 하나만 남는다. 키 행은 잔액 변경·거래 기록과 같은 트랜잭션에서
-- 커밋된다. (app/models.py IdempotencyKey)

CREATE TABLE IF NOT EXISTS idempotency_keys (
  id             INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id        INT          NOT NULL,
  `key`          VARCHAR(64)  NOT NULL,
  request_hash   VARCHAR(64)  NOT NULL,
  transaction_id INT          NOT NULL,
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_idempotency_user_key UNIQUE (user_id, `key`),
  CONSTRAINT fk_idempotency_user        FOREIGN KEY (user_id)        REFERENCES users (id),
  CONSTRAINT fk_idempotency_transaction FOREIGN KEY (transaction_id) REFERENCES transactions (id)
);
