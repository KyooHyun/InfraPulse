#!/usr/bin/env python
"""원장 대사 실행 — 불일치가 있으면 목록을 출력하고 종료 코드 1.

    python scripts/reconcile.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.reconciliation import reconcile


def main() -> int:
    db = SessionLocal()
    try:
        report = reconcile(db)
    finally:
        db.close()

    print(f"계좌 {report.accounts_checked:,}개, 분개 {report.journals_checked:,}건 검사")
    for journal_id, total in report.unbalanced_journals:
        print(f"  [분개 불균형] {journal_id}: 엔트리 합 {total}")
    for account_id, balance, expected in report.balance_mismatches:
        print(f"  [잔액 불일치] {account_id}: 잔액 {balance} / 엔트리 누적 {expected} (차이 {balance - expected})")
    print("일치" if report.ok else "불일치 발견")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
