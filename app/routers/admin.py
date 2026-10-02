from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_role, get_password_hash
from ..schemas import AuditChainVerification, UserCreate, UserOut, AuditLogOut
from .. import models, audit

router = APIRouter(prefix="/admin", tags=["관리자"])


# ── 사용자 관리 ───────────────────────────────────────────────────────────────

@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="사용자 생성")
def create_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    if db.query(models.User).filter(models.User.username == user_in.username).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 사용자명입니다")
    if user_in.role not in ("STAFF", "RISK_OFFICER", "ADMIN"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="유효하지 않은 역할입니다")

    user = models.User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
        role=user_in.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    audit.log_event(
        db,
        action="CREATE_USER",
        entity_type="User",
        entity_id=str(user.id),
        detail=f"username={user.username}, role={user.role}",
        user_id=current_user.id,
    )
    return user


@router.get("/users", response_model=List[UserOut], summary="사용자 목록")
def list_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    return db.query(models.User).order_by(models.User.id).all()


@router.put("/users/{user_id}/deactivate", response_model=UserOut, summary="사용자 비활성화")
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자기 자신을 비활성화할 수 없습니다")

    user.is_active = False
    db.commit()
    db.refresh(user)

    audit.log_event(
        db,
        action="DEACTIVATE_USER",
        entity_type="User",
        entity_id=str(user_id),
        user_id=current_user.id,
    )
    return user


# ── 감사 로그 ─────────────────────────────────────────────────────────────────

@router.get("/audit-logs", response_model=List[AuditLogOut], summary="감사 로그 조회")
def list_audit_logs(
    skip: int = Query(0, ge=0, description="건너뛸 건수"),
    limit: int = Query(100, ge=1, le=1000, description="최대 반환 건수"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    return (
        db.query(models.AuditLog)
        .order_by(models.AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/audit-logs/verify",
    response_model=AuditChainVerification,
    summary="감사 로그 해시 체인 검증",
)
def verify_audit_chain(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_role("ADMIN")),
):
    """감사 로그가 기록된 이후 수정·삭제되지 않았음을 해시 체인으로 확인한다.

    status가 BROKEN이면 broken_at이 어느 행에서 무엇이 어긋났는지 알려준다.
    이 엔드포인트 자체는 감사 로그를 남기지 않는다 — 검증이 검증 대상을 늘리면
    체인이 계속 자라 같은 결과를 두 번 확인할 수 없다.
    """
    return audit.verify_chain(db)
