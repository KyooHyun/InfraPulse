from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_role
from ..schemas import FdsAlertOut, FdsDecisionCreate, FdsDecisionOut, FdsRuleOut, FdsRuleUpdate
from .. import models, audit

router = APIRouter(prefix="/fds", tags=["FDS"])


# ── FDS 알림 ──────────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=List[FdsAlertOut], summary="FDS 알림 목록")
def list_alerts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    return (
        db.query(models.FdsAlert)
        .order_by(models.FdsAlert.created_at.desc())
        .limit(200)
        .all()
    )


@router.get("/alerts/{alert_id}", response_model=FdsAlertOut, summary="FDS 알림 상세")
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    alert = db.query(models.FdsAlert).filter(models.FdsAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")
    return alert


@router.post(
    "/alerts/{alert_id}/review",
    response_model=FdsDecisionOut,
    summary="FDS 알림 검토 (승인/기각)",
)
def review_alert(
    alert_id: int,
    decision_in: FdsDecisionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    """
    이상거래 알림을 검토한다.
    - decision=APPROVE: 정상 거래로 판단 → 알림 상태 APPROVED
    - decision=REJECT:  이상거래 확정 → 알림 상태 REJECTED
    """
    alert = db.query(models.FdsAlert).filter(models.FdsAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")
    if alert.status not in ("DETECTED", "UNDER_REVIEW"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 처리된 알림입니다")
    if decision_in.decision not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="decision은 APPROVE 또는 REJECT")

    decision = models.FdsDecision(
        alert_id=alert_id,
        reviewer_id=current_user.id,
        decision=decision_in.decision,
        comment=decision_in.comment,
    )
    db.add(decision)

    alert.status = "APPROVED" if decision_in.decision == "APPROVE" else "REJECTED"
    alert.reviewed_at = datetime.now(timezone.utc)
    alert.reviewed_by = current_user.id
    db.commit()
    db.refresh(decision)

    audit.log_event(
        db,
        action=f"FDS_ALERT_{decision_in.decision}",
        entity_type="FdsAlert",
        entity_id=str(alert_id),
        detail=decision_in.comment,
        user_id=current_user.id,
    )
    return decision


# ── FDS 룰 관리 (ADMIN 전용) ──────────────────────────────────────────────────

@router.get("/rules", response_model=List[FdsRuleOut], summary="FDS 룰 목록")
def list_rules(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    return db.query(models.FdsRule).order_by(models.FdsRule.id).all()


@router.put("/rules/{rule_id}", response_model=FdsRuleOut, summary="FDS 룰 수정")
def update_rule(
    rule_id: int,
    rule_update: FdsRuleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    """임계값, 가중치, 활성화 여부를 운영 중 변경할 수 있다."""
    rule = db.query(models.FdsRule).filter(models.FdsRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규칙을 찾을 수 없습니다")

    if rule_update.threshold is not None:
        rule.threshold = rule_update.threshold
    if rule_update.weight is not None:
        rule.weight = rule_update.weight
    if rule_update.is_active is not None:
        rule.is_active = rule_update.is_active

    db.commit()
    db.refresh(rule)

    audit.log_event(
        db,
        action="UPDATE_FDS_RULE",
        entity_type="FdsRule",
        entity_id=str(rule_id),
        detail=f"threshold={rule.threshold}, weight={rule.weight}, is_active={rule.is_active}",
        user_id=current_user.id,
    )
    return rule
