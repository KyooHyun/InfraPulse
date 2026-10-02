"""컴플라이언스 보고서 자동 생성 유틸리티.

특정금융정보법(특금법) 기반:
  STR (의심거래보고) — FDS 고위험 거래 보고
  CTR (고액현금거래보고) — 이 시스템에서는 생성하지 않는다. CTR 대상은 **현금**
      입출금이고 **동일인 1거래일 합산** 1천만원 이상이다(제4조의2). 지금은 계좌이체만
      있고 계좌를 고객 단위로 묶는 식별자도 없어서, 현금 거래 유형과 고객 식별자가
      추가될 때 구현한다. 예전에는 이체 한 건이 1천만원 이상이면 CTR을 만들었는데,
      제도와 다른 동작이었다.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models
from .metrics import compliance_report_total


def _report_number(report_type: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:8].upper()
    return f"{report_type}-{date_str}-{suffix}"


def create_str(
    db: Session,
    tx: models.Transaction,
    reason: str,
) -> models.ComplianceReport:
    """FDS 고위험 거래에 대해 STR을 생성한다. 동일 거래에 중복 생성하지 않는다."""
    existing = (
        db.query(models.ComplianceReport)
        .filter(
            models.ComplianceReport.transaction_id == tx.id,
            models.ComplianceReport.report_type == "STR",
        )
        .first()
    )
    if existing:
        return existing

    report = models.ComplianceReport(
        report_type="STR",
        transaction_id=tx.id,
        account_from=tx.account_from,
        account_to=tx.account_to,
        amount=tx.amount,
        currency=tx.currency,
        reason=reason,
        report_number=_report_number("STR"),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    compliance_report_total.labels(report_type="STR").inc()
    return report
