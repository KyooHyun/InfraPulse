#!/usr/bin/env python
"""PaySim CSV → InfraPulse DB 적재.

PaySim 다운로드: https://www.kaggle.com/datasets/ealaxi/paysim1
  (PS_20174392719_1491204439457_log.csv, 약 6.3 M 행)

Usage:
    python scripts/load_paysim.py <csv_path> [--limit N]

TRANSFER / CASH_OUT 유형만 적재한다 (사기 거래가 이 두 유형에만 존재).
--limit은 빠른 테스트용 (예: --limit 100000).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.db import SessionLocal
from app import models

BASE_DT = datetime(2024, 1, 1, tzinfo=timezone.utc)
FRAUD_TYPES = {"TRANSFER", "CASH_OUT"}
CHUNK_SIZE = 5_000


def _step_to_dt(step: int) -> datetime:
    return BASE_DT + timedelta(hours=int(step))


def load(csv_path: str, limit: int | None = None) -> None:
    print(f"PaySim 적재 시작: {csv_path}")
    if limit:
        print(f"  (최대 {limit:,} 행)")

    reader = pd.read_csv(csv_path, chunksize=CHUNK_SIZE)
    db = SessionLocal()
    total = fraud = 0

    try:
        for chunk in reader:
            if limit and total >= limit:
                break

            subset = chunk[chunk["type"].isin(FRAUD_TYPES)]
            if limit:
                subset = subset.head(limit - total)
            if subset.empty:
                continue

            rows = [
                models.Transaction(
                    account_from=str(row["nameOrig"]),
                    account_to=str(row["nameDest"]),
                    amount=float(row["amount"]),
                    currency="USD",
                    status="success",
                    reason="paysim",
                    risk_score=0.0,
                    transaction_type=str(row["type"]),
                    balance_orig_before=float(row["oldbalanceOrg"]),
                    balance_orig_after=float(row["newbalanceOrig"]),
                    balance_dest_before=float(row["oldbalanceDest"]),
                    balance_dest_after=float(row["newbalanceDest"]),
                    is_fraud=bool(row["isFraud"]),
                    created_at=_step_to_dt(row["step"]),
                )
                for _, row in subset.iterrows()
            ]

            db.bulk_save_objects(rows)
            db.commit()
            total += len(rows)
            fraud += sum(1 for r in rows if r.is_fraud)
            print(f"  {total:,} 행 적재 완료 (사기: {fraud}건)")

    finally:
        db.close()

    rate = fraud / total * 100 if total else 0
    print(f"\n완료: {total:,}건 적재 / 사기 {fraud}건 ({rate:.2f}%)")
    print("다음 단계: python scripts/train_model.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="PaySim CSV를 InfraPulse DB에 적재한다")
    parser.add_argument("csv", help="PaySim CSV 파일 경로")
    parser.add_argument("--limit", type=int, default=None, help="적재 행 수 상한 (테스트용)")
    args = parser.parse_args()
    load(args.csv, args.limit)


if __name__ == "__main__":
    main()
