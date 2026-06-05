from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import create_access_token, verify_password, get_current_user
from ..schemas import Token, UserOut
from ..metrics import login_failed_total, anomaly_event_total, anomaly_login_failure_total, fds_alert_total
from .. import models, audit

router = APIRouter(prefix="/auth", tags=["인증"])

# 인메모리 로그인 실패 추적 (계정별 슬라이딩 윈도우)
_login_failures: Dict[str, deque] = defaultdict(deque)
LOGIN_FAILURE_THRESHOLD = 3
LOGIN_FAILURE_WINDOW_SECONDS = 300


@router.post("/token", response_model=Token, summary="JWT 토큰 발급")
def login_for_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    ip = request.client.host if request.client else None

    if not user or not verify_password(form_data.password, user.hashed_password):
        login_failed_total.inc()

        # 로그인 실패 횟수 추적
        now = datetime.now(timezone.utc)
        failures = _login_failures[form_data.username]
        failures.append(now)
        cutoff = now - timedelta(seconds=LOGIN_FAILURE_WINDOW_SECONDS)
        while failures and failures[0] < cutoff:
            failures.popleft()

        # 임계값 초과 시 FDS 알림 생성
        if len(failures) >= LOGIN_FAILURE_THRESHOLD:
            anomaly_event_total.inc()
            anomaly_login_failure_total.inc()
            fds_alert = models.FdsAlert(
                alert_type="LOGIN_FAILURE",
                risk_score=20.0,
                status="DETECTED",
                detail=(
                    f"계정 '{form_data.username}'에서 {LOGIN_FAILURE_WINDOW_SECONDS}초 내 "
                    f"{len(failures)}회 로그인 실패 (IP: {ip})"
                ),
            )
            db.add(fds_alert)
            db.commit()
            fds_alert_total.labels(alert_type="LOGIN_FAILURE").inc()

        audit.log_event(
            db, action="LOGIN_FAILURE",
            detail=f"username={form_data.username}",
            ip_address=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="아이디 또는 비밀번호가 올바르지 않습니다",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": user.username, "role": user.role})
    audit.log_event(
        db, action="LOGIN_SUCCESS",
        entity_type="User", entity_id=str(user.id),
        detail=f"username={user.username}",
        ip_address=ip,
        user_id=user.id,
    )
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserOut, summary="내 계정 정보")
def get_me(current_user: models.User = Depends(get_current_user)):
    return current_user
