"""컴플라이언스 보고서 자동 생성 유틸리티.

특정금융정보법(특금법) 기반:
  CTR (고액현금거래보고) — 1천만원 이상 거래 자동 보고
  STR (의심거래보고)    — FDS 고위험 거래 보고
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import models
from .metrics import compliance_report_total

CTR_THRESHOLD = 10_000_000.0  # 특금법 제4조: 1천만원


def _report_number(report_type: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:8].upper()
    return f"{report_type}-{date_str}-{suffix}"


def try_create_ctr(
    db: Session,
    tx: models.Transaction,
) -> Optional[models.ComplianceReport]:
    """1천만원 이상이면 CTR을 생성한다. 이미 있으면 중복 생성하지 않는다."""
    if tx.amount < CTR_THRESHOLD:
        return None

    existing = (
        db.query(models.ComplianceReport)
        .filter(
            models.ComplianceReport.transaction_id == tx.id,
            models.ComplianceReport.report_type == "CTR",
        )
        .first()
    )
    if existing:
        return existing

    report = models.ComplianceReport(
        report_type="CTR",
        transaction_id=tx.id,
        account_from=tx.account_from,
        account_to=tx.account_to,
        amount=tx.amount,
        currency=tx.currency,
        reason=f"고액현금거래 자동 보고 (기준: {int(CTR_THRESHOLD):,}원 이상)",
        report_number=_report_number("CTR"),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    compliance_report_total.labels(report_type="CTR").inc()
    return report


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
