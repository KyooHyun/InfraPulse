from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_role
from ..schemas import (
    FdsAlertOut, FdsDecisionCreate, FdsDecisionOut,
    FdsRuleOut, FdsRuleUpdate, FdsStatsOut,
)
from .. import models, audit
from ..fds_engine import RISK_LEVEL_HIGH

router = APIRouter(prefix="/fds", tags=["FDS"])


# ── FDS 알림 ──────────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=List[FdsAlertOut], summary="FDS 알림 목록")
def list_alerts(
    skip: int = Query(0, ge=0, description="건너뛸 건수"),
    limit: int = Query(100, ge=1, le=500, description="최대 반환 건수"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    return (
        db.query(models.FdsAlert)
        .order_by(models.FdsAlert.created_at.desc())
        .offset(skip)
        .limit(limit)
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


# ── FDS 운영 통계 ─────────────────────────────────────────────────────────────

@router.get("/stats", response_model=FdsStatsOut, summary="FDS 운영 통계")
def get_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    """
    FDS 시스템의 정량적 운영 지표를 반환한다.

    - **detection_rate_pct**: 전체 거래 대비 FDS 알림 발생률 (탐지율)
    - **false_positive_rate_pct**: 검토 완료 알림 중 정상 판정 비율 (오탐율)
    - **rule_coverage_pct**: 활성 룰 중 실제 알림을 발생시킨 룰 유형 비율
    - **high_risk_rate_pct**: 전체 거래 중 고위험(score≥70) 거래 비율
    """
    # 거래 통계
    total_txs = db.query(models.Transaction).count()
    failed_txs = db.query(models.Transaction).filter(models.Transaction.status == "failed").count()

    # FDS 알림 통계
    total_alerts = db.query(models.FdsAlert).count()
    pending = db.query(models.FdsAlert).filter(models.FdsAlert.status == "DETECTED").count()
    approved = db.query(models.FdsAlert).filter(models.FdsAlert.status == "APPROVED").count()
    rejected = db.query(models.FdsAlert).filter(models.FdsAlert.status == "REJECTED").count()

    # 알림 유형별 집계
    alerts_by_rule: dict = {}
    for alert_type, count in (
        db.query(models.FdsAlert.alert_type, func.count(models.FdsAlert.id))
        .group_by(models.FdsAlert.alert_type)
        .all()
    ):
        alerts_by_rule[alert_type] = count

    # 위험 점수 통계
    avg_score = db.query(func.avg(models.Transaction.risk_score)).scalar() or 0.0
    high_risk = db.query(models.Transaction).filter(
        models.Transaction.risk_score >= RISK_LEVEL_HIGH
    ).count()

    # 컴플라이언스 보고서
    str_total = db.query(models.ComplianceReport).filter(
        models.ComplianceReport.report_type == "STR"
    ).count()
    ctr_total = db.query(models.ComplianceReport).filter(
        models.ComplianceReport.report_type == "CTR"
    ).count()
    pending_compliance = db.query(models.ComplianceReport).filter(
        models.ComplianceReport.status == "PENDING"
    ).count()

    # 룰 커버리지: 활성 룰 수 vs 실제 알림이 발생한 룰 유형 수
    active_rules = db.query(models.FdsRule).filter(models.FdsRule.is_active.is_(True)).count()
    triggered_types = len(alerts_by_rule)

    # 비율 계산
    failure_rate = round(failed_txs / total_txs * 100, 2) if total_txs else 0.0
    detection_rate = round(total_alerts / total_txs * 100, 2) if total_txs else 0.0
    resolved = approved + rejected
    false_positive_rate: Optional[float] = round(approved / resolved * 100, 2) if resolved else None
    high_risk_rate = round(high_risk / total_txs * 100, 2) if total_txs else 0.0
    rule_coverage = round(triggered_types / active_rules * 100, 2) if active_rules else 0.0

    return FdsStatsOut(
        total_transactions=total_txs,
        failed_transactions=failed_txs,
        failure_rate_pct=failure_rate,
        total_alerts=total_alerts,
        pending_review=pending,
        detection_rate_pct=detection_rate,
        false_positive_rate_pct=false_positive_rate,
        alerts_by_rule=alerts_by_rule,
        avg_risk_score=round(float(avg_score), 2),
        high_risk_count=high_risk,
        high_risk_rate_pct=high_risk_rate,
        str_total=str_total,
        ctr_total=ctr_total,
        pending_compliance=pending_compliance,
        active_rule_count=active_rules,
        triggered_rule_types=triggered_types,
        rule_coverage_pct=rule_coverage,
    )
