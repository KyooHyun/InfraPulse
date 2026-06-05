from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import get_current_user, require_role
from ..schemas import KycRecordCreate, KycRecordOut
from .. import models, audit

router = APIRouter(prefix="/kyc", tags=["KYC"])


@router.post("", response_model=KycRecordOut, status_code=status.HTTP_201_CREATED, summary="KYC 등록")
def register_kyc(
    kyc_in: KycRecordCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    계좌 개설 시 고객 신원을 등록한다 (고객확인제도, KYC).
    id_number_masked는 마스킹된 값을 받아 저장한다 — 원문 식별번호는 절대 저장하지 않는다.
    """
    if db.query(models.KycRecord).filter(models.KycRecord.account_id == kyc_in.account_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 등록된 계좌입니다")

    record = models.KycRecord(
        account_id=kyc_in.account_id,
        customer_name=kyc_in.customer_name,
        id_type=kyc_in.id_type,
        id_number_masked=kyc_in.id_number_masked,
        risk_grade=kyc_in.risk_grade or "LOW",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    audit.log_event(
        db,
        action="KYC_REGISTER",
        entity_type="KycRecord",
        entity_id=record.account_id,
        detail=f"customer={kyc_in.customer_name}, id_type={kyc_in.id_type}, risk_grade={record.risk_grade}",
        user_id=current_user.id,
    )
    return record


@router.get("/{account_id}", response_model=KycRecordOut, summary="KYC 조회")
def get_kyc(
    account_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    record = db.query(models.KycRecord).filter(models.KycRecord.account_id == account_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KYC 정보를 찾을 수 없습니다")
    return record


@router.put("/{account_id}/verify", response_model=KycRecordOut, summary="KYC 승인")
def verify_kyc(
    account_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    record = db.query(models.KycRecord).filter(models.KycRecord.account_id == account_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KYC 정보를 찾을 수 없습니다")
    if record.verification_status == "VERIFIED":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 인증된 계좌입니다")

    record.verification_status = "VERIFIED"
    record.verified_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    audit.log_event(
        db,
        action="KYC_VERIFY",
        entity_type="KycRecord",
        entity_id=account_id,
        detail=f"customer={record.customer_name}",
        user_id=current_user.id,
    )
    return record


@router.get("", response_model=List[KycRecordOut], summary="KYC 전체 목록")
def list_kyc(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("RISK_OFFICER", "ADMIN")),
):
    return db.query(models.KycRecord).order_by(models.KycRecord.created_at.desc()).all()
