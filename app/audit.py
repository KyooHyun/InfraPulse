"""불변 감사 추적 유틸리티.

모든 중요 이벤트(로그인, 거래 생성, FDS 검토, 컴플라이언스 보고 등)를
audit_logs 테이블에 기록한다. 행은 INSERT 전용이며 수정/삭제하지 않는다.
각 행에 SHA-256 체크섬을 포함해 위변조 여부를 검증할 수 있다.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import models


def _checksum(data: dict) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def log_event(
    db: Session,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
) -> models.AuditLog:
    payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    entry = models.AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
        ip_address=ip_address,
        checksum=_checksum(payload),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
