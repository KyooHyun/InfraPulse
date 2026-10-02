from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_role
from ..schemas import ComplianceReportOut, ComplianceReviewCreate
from .. import models, audit

router = APIRouter(prefix="/compliance", tags=["컴플라이언스"])


@router.get("/reports", response_model=List[ComplianceReportOut], summary="보고서 목록 (STR)")
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


def _get_report(db: Session, report_id: int) -> models.ComplianceReport:
    report = (
        db.query(models.ComplianceReport)
        .filter(models.ComplianceReport.id == report_id)
        .first()
    )
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="보고서를 찾을 수 없습니다")
    return report


@router.post(
    "/reports/{report_id}/review",
    response_model=ComplianceReportOut,
    summary="보고서 초안 검토 (보고 대상 여부 판단)",
)
def review_report(
    report_id: int,
    body: ComplianceReviewCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    """
    DRAFT 보고서를 담당자가 판단한다 — APPROVE면 APPROVED(제출 대기),
    DISMISS면 DISMISSED(보고 불필요). 판단 사유와 검토자는 감사 로그에 남는다.
    STR은 "의심되는 합당한 근거"에 대한 사람의 판단이 요건이라, 점수만으로는 제출하지 않는다.
    """
    report = _get_report(db, report_id)
    if report.status != "DRAFT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 검토된 보고서입니다")

    report.status = "APPROVED" if body.decision == "APPROVE" else "DISMISSED"
    db.commit()
    db.refresh(report)

    audit.log_event(
        db,
        action=f"REVIEW_{report.report_type}_{body.decision}",
        entity_type="ComplianceReport",
        entity_id=str(report_id),
        detail=f"report_number={report.report_number}, comment={body.comment}",
        user_id=current_user.id,
    )
    return report


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
    검토에서 APPROVED된 보고서만 SUBMITTED로 전환한다.
    실제 환경에서는 금융정보분석원(KoFIU) API 연동으로 대체.
    """
    report = _get_report(db, report_id)
    if report.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"검토 승인된 보고서만 제출할 수 있습니다 (현재 상태: {report.status})",
        )

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
