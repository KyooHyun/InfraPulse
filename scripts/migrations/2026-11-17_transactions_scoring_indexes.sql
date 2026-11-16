-- FDS 채점 쿼리용 인덱스 (app/models.py Transaction.__table_args__와 같다).
-- 이 변경 이전에 만든 DB에서 한 번 실행한다. 새로 만드는 DB는 앱 기동 시 create_all이 만든다.
-- MySQL 8.0은 보조 인덱스를 온라인(INPLACE, 쓰기 허용)으로 만든다.
CREATE INDEX ix_transactions_from_created ON transactions (account_from, created_at);
CREATE INDEX ix_transactions_to_created   ON transactions (account_to, created_at);
CREATE INDEX ix_transactions_created_id   ON transactions (created_at, id);
