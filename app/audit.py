"""불변 감사 추적 — SHA-256 해시 체인.

모든 중요 이벤트(로그인, 거래 생성, FDS 검토, 컴플라이언스 보고 등)를 audit_logs
테이블에 기록한다. 행은 INSERT 전용이며 수정/삭제하지 않는다.

**왜 체인인가.** 이전 구현은 행마다 독립적인 SHA-256 체크섬을 저장했다. 그러면
행 하나를 고쳤을 때는 체크섬이 어긋나 탐지되지만, 행을 **통째로 지우면** 남은
행들의 체크섬이 전부 자기 자신과 맞아떨어져 아무 이상이 없어 보인다. 감사 추적에서
가장 흔한 은폐 수법이 정확히 그것이다 — 불리한 기록만 골라 삭제하기.
그 상태로 "불변 감사 추적"이라고 부르는 것은 사실이 아니었다.

각 행의 해시 입력에 **직전 행의 해시**를 넣으면 행들이 한 줄로 엮인다. 중간 행을
지우면 그 다음 행의 prev_checksum이 새 이웃과 맞지 않고, 행을 끼워 넣거나 순서를
바꿔도 마찬가지다. 마지막 행들을 지우는 경우만 남은 행끼리는 멀쩡하므로,
AuditChainHead에 머리 해시를 따로 보관해 그것까지 잡는다.

한계는 분명히 해 둔다. 체인은 **위변조를 탐지**할 뿐 **막지는 못한다.** DB 쓰기
권한을 가진 사람은 행을 지운 뒤 그 이후 행 전부와 머리 해시를 다시 계산해 넣을 수
있다. 그것까지 막으려면 머리 해시를 주기적으로 외부(다른 시스템·다른 권한 도메인)에
고정해야 한다. 지금 구현은 "DB 안에서의 조용한 삭제·수정은 반드시 드러난다"까지다.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models

# 첫 행의 prev_checksum. 체인의 시작을 나타내는 고정값이다.
GENESIS_CHECKSUM = "0" * 64

HEAD_ID = 1


def _canonical(payload: Dict[str, Any]) -> str:
    """해시 입력의 정규 표현. 키 순서가 달라져도 같은 문자열이 나와야 한다."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _checksum(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _payload(
    prev_checksum: str,
    event_time: str,
    action: str,
    entity_type: Optional[str],
    entity_id: Optional[str],
    detail: Optional[str],
    ip_address: Optional[str],
    user_id: Optional[int],
) -> Dict[str, Any]:
    """해시가 보호하는 필드 전체.

    detail과 ip_address도 포함한다. 이전 구현은 이 둘을 빼고 해시해서, "관리자가
    자기 계정으로 무엇을 했는지"가 적힌 detail을 고쳐도 체크섬이 그대로였다.
    해시가 보호하지 않는 필드는 감사 증거로 쓸 수 없다.
    """
    return {
        "prev_checksum": prev_checksum,
        "event_time": event_time,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "detail": detail,
        "ip_address": ip_address,
        "user_id": user_id,
    }


def ensure_chain_head(db: Session) -> None:
    """체인 머리 행을 만든다. 앱 기동 시 한 번 호출하면 된다."""
    if db.query(models.AuditChainHead.id).filter(models.AuditChainHead.id == HEAD_ID).first():
        return
    db.add(
        models.AuditChainHead(
            id=HEAD_ID,
            head_checksum=GENESIS_CHECKSUM,
            entry_count=0,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # 다른 워커가 먼저 만들었다.
        db.rollback()


def _lock_head(db: Session) -> models.AuditChainHead:
    """체인 머리에 배타 잠금을 건다 — 여기가 유일한 직렬화 지점이다."""
    head = (
        db.query(models.AuditChainHead)
        .filter(models.AuditChainHead.id == HEAD_ID)
        .with_for_update()
        .one_or_none()
    )
    if head is None:
        ensure_chain_head(db)
        head = (
            db.query(models.AuditChainHead)
            .filter(models.AuditChainHead.id == HEAD_ID)
            .with_for_update()
            .one()
        )
    return head


def log_event(
    db: Session,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
) -> models.AuditLog:
    """이벤트 한 건을 체인 끝에 붙인다."""
    head = _lock_head(db)
    prev_checksum = head.head_checksum or GENESIS_CHECKSUM

    event_time = datetime.now(timezone.utc).isoformat()
    payload = _payload(
        prev_checksum, event_time, action, entity_type, entity_id,
        detail, ip_address, user_id,
    )
    checksum = _checksum(payload)

    entry = models.AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
        ip_address=ip_address,
        checksum=checksum,
        prev_checksum=prev_checksum,
        event_time=event_time,
    )
    db.add(entry)

    head.head_checksum = checksum
    head.entry_count = (head.entry_count or 0) + 1

    db.commit()
    db.refresh(entry)
    return entry


def recompute_checksum(entry: models.AuditLog) -> str:
    """저장된 행에서 해시를 다시 계산한다 — 검증과 기록이 같은 함수를 쓰게 한다."""
    return _checksum(
        _payload(
            entry.prev_checksum,
            entry.event_time,
            entry.action,
            entry.entity_type,
            entry.entity_id,
            entry.detail,
            entry.ip_address,
            entry.user_id,
        )
    )


def verify_chain(db: Session, limit: Optional[int] = None) -> Dict[str, Any]:
    """전체 체인을 처음부터 검증한다.

    탐지하는 위변조와 그 신호:
      · 행 내용 수정   → 다시 계산한 해시가 저장된 해시와 다르다 (CHECKSUM_MISMATCH)
      · 중간 행 삭제·삽입·순서 변경 → 다음 행의 prev_checksum이 이웃과 안 맞는다 (BROKEN_LINK)
      · 마지막 행 삭제 → 남은 행끼리는 멀쩡하나 머리 해시가 안 맞는다 (HEAD_MISMATCH)

    체인 도입 이전에 쓰인 행(prev_checksum이 NULL)은 LEGACY로 따로 센다.
    소급해서 체인에 넣을 방법은 없다 — 그러려면 과거 해시를 다시 계산해야 하는데,
    그건 이 체인이 막으려는 행위와 구분되지 않는다.
    """
    query = db.query(models.AuditLog).order_by(models.AuditLog.id.asc())
    if limit is not None:
        query = query.limit(limit)
    entries: List[models.AuditLog] = query.all()

    head = db.query(models.AuditChainHead).filter(models.AuditChainHead.id == HEAD_ID).one_or_none()
    head_checksum = head.head_checksum if head else None

    legacy = [e for e in entries if e.prev_checksum is None]
    chained = [e for e in entries if e.prev_checksum is not None]

    result: Dict[str, Any] = {
        "status": "OK",
        "entries_total": len(entries),
        "entries_chained": len(chained),
        "entries_legacy": len(legacy),
        "head_checksum": head_checksum,
        "broken_at": None,
    }

    expected_prev = GENESIS_CHECKSUM
    for entry in chained:
        if entry.prev_checksum != expected_prev:
            result["status"] = "BROKEN"
            result["broken_at"] = {
                "id": entry.id,
                "reason": "BROKEN_LINK",
                "detail": "직전 행의 해시와 연결되지 않는다 — 앞선 행이 삭제·삽입·재배열됐다",
                "expected_prev_checksum": expected_prev,
                "stored_prev_checksum": entry.prev_checksum,
            }
            return result

        recomputed = recompute_checksum(entry)
        if recomputed != entry.checksum:
            result["status"] = "BROKEN"
            result["broken_at"] = {
                "id": entry.id,
                "reason": "CHECKSUM_MISMATCH",
                "detail": "행 내용이 기록 당시와 다르다 — 값이 수정됐다",
                "expected_checksum": recomputed,
                "stored_checksum": entry.checksum,
            }
            return result

        expected_prev = entry.checksum

    # 부분 검증(limit)에서는 머리 해시가 안 맞는 게 정상이다.
    if limit is None and chained and head_checksum not in (None, expected_prev):
        result["status"] = "BROKEN"
        result["broken_at"] = {
            "id": chained[-1].id,
            "reason": "HEAD_MISMATCH",
            "detail": "마지막 행이 체인 머리와 다르다 — 꼬리 쪽 행이 삭제됐다",
            "expected_head_checksum": expected_prev,
            "stored_head_checksum": head_checksum,
        }

    return result
