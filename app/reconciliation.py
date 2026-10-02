"""원장 대사 — 잔액 컬럼과 복식부기 엔트리가 서로 맞는지 검사한다.

    python -m scripts.reconcile        (스케줄러는 두지 않는다 — 필요할 때 돌린다)

검사하는 것 두 가지:
  1) 분개마다 엔트리 합이 0인가 — 한쪽 다리만 기록됐거나 지워졌으면 돈이 생기거나 사라진 것이다
  2) 계좌 잔액이 그 계좌 엔트리의 누적과 같은가 — 잔액 컬럼이 분개 없이 바뀌었다는 뜻이다
     (직접 UPDATE, 버그, 위변조). 잔액 컬럼은 이체 경로가 빠르게 읽고 쓰기 위한 캐시이고,
     엔트리가 근거다.

불일치는 고치지 않고 보고만 한다. 어느 쪽이 맞는지는 사람이 감사 로그와 함께 판단해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import ledger, models


@dataclass
class ReconciliationReport:
    accounts_checked: int = 0
    journals_checked: int = 0
    unbalanced_journals: List[Tuple[str, Decimal]] = field(default_factory=list)          # (분개, 합)
    balance_mismatches: List[Tuple[str, Decimal, Decimal]] = field(default_factory=list)  # (계좌, 잔액, 엔트리 누적)

    @property
    def ok(self) -> bool:
        return not self.unbalanced_journals and not self.balance_mismatches


def _money(value) -> Decimal:
    return ledger.to_money(value if value is not None else 0)


def reconcile(db: Session) -> ReconciliationReport:
    report = ReconciliationReport()

    journal_sums = (
        db.query(models.LedgerEntry.journal_id, func.sum(models.LedgerEntry.amount))
        .group_by(models.LedgerEntry.journal_id)
        .all()
    )
    report.journals_checked = len(journal_sums)
    report.unbalanced_journals = [
        (journal_id, _money(total)) for journal_id, total in journal_sums if _money(total) != 0
    ]

    entry_totals = dict(
        db.query(models.LedgerEntry.account_id, func.sum(models.LedgerEntry.amount))
        .group_by(models.LedgerEntry.account_id)
        .all()
    )
    accounts = db.query(models.Account.account_id, models.Account.balance).all()
    report.accounts_checked = len(accounts)
    for account_id, balance in accounts:
        expected = _money(entry_totals.get(account_id))
        if _money(balance) != expected:
            report.balance_mismatches.append((account_id, _money(balance), expected))
    return report
