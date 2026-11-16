#!/usr/bin/env python
"""부하 테스트 DB 준비 — 과거 거래를 대량으로 채우고, 채점 인덱스를 켜고 끄고, 채점 쿼리의 EXPLAIN을 찍는다.

    python scripts/loadtest/seed_large.py --rows 1000000        # 테이블 생성 + 과거 거래 100만 행
    python scripts/loadtest/seed_large.py --indexes off          # 채점 인덱스 3개 삭제 (전후 비교의 '전')
    python scripts/loadtest/seed_large.py --indexes on           # 다시 생성
    python scripts/loadtest/seed_large.py --explain              # 채점 쿼리 3개의 실행 계획

과거 거래는 transactions에만 넣는다(ledger_entries·accounts는 건드리지 않는다). 채점 쿼리가 읽는 것은
transactions뿐이고, 목적은 "거래가 쌓였을 때 채점 비용"이다. 그래서 이 DB에서는 대사가 맞지 않는다.

분포: 송금·수취 계좌는 과거 계좌 20만 개(HIST-*)에서, 시각은 최근 30일에 고르게, 상태는 90% 성공.
부하 테스트 계좌(LT-*)의 과거 거래는 넣지 않는다 — 넣으면 VELOCITY·NEW_RECIPIENT 값이 달라져 채점 결과가
구성마다 달라진다. 인덱스가 하는 일은 "LT 계좌의 행을 찾으려고 100만 행을 다 읽지 않는 것"이다.
DB 접속은 운영과 같은 환경 변수(MYSQL_HOST 등)를 쓴다.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from app import models  # noqa: F401 — 테이블 정의를 Base.metadata에 올린다
from app.db import Base, engine

INDEXES = {
    "ix_transactions_from_created": "(account_from, created_at)",
    "ix_transactions_to_created": "(account_to, created_at)",
    "ix_transactions_created_id": "(created_at, id)",
}
# collect_signals와 같은 모양의 쿼리 — 값만 고정했다
EXPLAIN_QUERIES = {
    "FAILURE_RATE (최근 50건)":
        "SELECT status FROM transactions ORDER BY created_at DESC, id DESC LIMIT 50",
    "VELOCITY (송금 계좌 10분)":
        "SELECT count(*) FROM transactions WHERE account_from = 'LT-0001' AND created_at >= NOW() - INTERVAL 10 MINUTE",
    "NEW_RECIPIENT (수취 계좌 24시간)":
        "SELECT count(*) FROM transactions WHERE account_to = 'LT-0002' AND created_at >= NOW() - INTERVAL 24 HOUR",
}


def seed(rows: int, batch: int = 10_000) -> None:
    Base.metadata.create_all(bind=engine)
    rng = random.Random(7)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sql = text(
        "INSERT INTO transactions (account_from, account_to, amount, currency, status, reason, risk_score, created_at) "
        "VALUES (:f, :t, :a, 'KRW', :s, :r, 0, :c)"
    )
    started = time.perf_counter()
    with engine.begin() as conn:
        for start in range(0, rows, batch):
            params = []
            for _ in range(min(batch, rows - start)):
                ok = rng.random() < 0.9
                params.append({
                    "f": f"HIST-{rng.randrange(200_000):06d}", "t": f"HIST-{rng.randrange(200_000):06d}",
                    "a": rng.choice([1_000, 5_000, 12_000, 30_000, 250_000]),
                    "s": "success" if ok else "failed", "r": "completed" if ok else "random failure",
                    "c": now - timedelta(seconds=rng.randrange(30 * 24 * 3600) + 3600),   # 최근 30일, 1시간 전까지
                })
            conn.execute(sql, params)
    print(f"과거 거래 {rows:,}행 적재 {time.perf_counter() - started:.0f}초")


def set_indexes(on: bool) -> None:
    with engine.begin() as conn:
        existing = {row[2] for row in conn.execute(text("SHOW INDEX FROM transactions"))}
        for name, cols in INDEXES.items():
            if on and name not in existing:
                conn.execute(text(f"CREATE INDEX {name} ON transactions {cols}"))
            elif not on and name in existing:
                conn.execute(text(f"DROP INDEX {name} ON transactions"))
        conn.execute(text("ANALYZE TABLE transactions"))
    print(f"채점 인덱스 {'켬' if on else '끔'}")


def explain() -> None:
    with engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM transactions")).scalar()
        print(f"transactions {total:,}행")
        for name, sql in EXPLAIN_QUERIES.items():
            row = conn.execute(text("EXPLAIN " + sql)).mappings().one()
            print(f"  {name:<32} type={row['type']:<6} key={str(row['key']):<30} rows={row['rows']:>9,}  extra={row['Extra']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--indexes", choices=("on", "off"))
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    if args.rows:
        seed(args.rows)
    if args.indexes:
        set_indexes(args.indexes == "on")
    if args.explain:
        explain()


if __name__ == "__main__":
    main()
