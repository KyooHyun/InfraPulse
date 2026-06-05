from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_role
from ..schemas import ComplianceReportOut
from .. import models, audit

router = APIRouter(prefix="/compliance", tags=["컴플라이언스"])


@router.get("/reports", response_model=List[ComplianceReportOut], summary="보고서 목록 (STR/CTR)")
def list_reports(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    return (
        db.query(models.ComplianceReport)
        .order_by(models.ComplianceReport.created_at.desc())
        .limit(200)
        .all()
    )


@router.post(
    "/reports/{report_id}/submit",
    response_model=ComplianceReportOut,
    summary="보고서 제출 처리",
)
def submit_report(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    """
    PENDING 상태 보고서를 SUBMITTED로 전환한다.
    실제 환경에서는 금융정보분석원(KoFIU) API 연동으로 대체.
    """
    report = (
        db.query(models.ComplianceReport)
        .filter(models.ComplianceReport.id == report_id)
        .first()
    )
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="보고서를 찾을 수 없습니다")
    if report.status != "PENDING":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 제출된 보고서입니다")

    report.status = "SUBMITTED"
    report.submitted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)

    audit.log_event(
        db,
        action=f"SUBMIT_{report.report_type}",
        entity_type="ComplianceReport",
        entity_id=str(report_id),
        detail=f"report_number={report.report_number}, amount={report.amount:,.0f}",
        user_id=current_user.id,
    )
    return report
