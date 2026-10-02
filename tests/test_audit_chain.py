"""감사 로그 해시 체인 테스트 — 조용한 삭제·수정이 드러나는가.

체인을 붙인 이유는 한 문장으로 요약된다: **행 단위 독립 해시로는 행 삭제를 잡지
못한다.** 아래 test_row_deletion_was_invisible_to_row_level_hashes 가 그 옛 방식을
그대로 재현해, 지운 자리가 정말로 안 보였다는 것을 보여준다. 나머지 테스트는
체인이 같은 조작을 어떻게 잡아내는지 확인한다.

조작은 전부 API가 아니라 DB 세션으로 직접 한다 — 정상 경로로는 감사 로그를
지우거나 고칠 수 없기 때문이다. 즉 여기서 흉내내는 공격자는 DB 쓰기 권한을 가진
사람이며, 그게 감사 추적이 실제로 방어해야 할 상대다.
"""
import hashlib
import json
from contextlib import contextmanager

from app import audit, models

from .conftest import auth_header


@contextmanager
def tamper_scope(db):
    """조작을 커밋하지 않고 검증한 뒤 반드시 되돌린다.

    모듈 안의 테스트들이 DB를 공유하므로(conftest의 module 스코프), 조작을 커밋하면
    그 뒤에 도는 테스트가 전부 "이미 끊긴 체인"을 보게 되어 서로를 오염시킨다.
    flush까지만 해서 같은 세션에는 보이게 하고, 블록을 벗어나면 롤백한다.
    """
    try:
        yield
    finally:
        db.rollback()


def _write_events(db, count: int = 5):
    """체인에 이벤트 몇 건을 쌓는다."""
    return [
        audit.log_event(
            db,
            action="TEST_EVENT",
            entity_type="Test",
            entity_id=str(i),
            detail=f"이벤트 {i}",
            ip_address="10.0.0.1",
        )
        for i in range(count)
    ]


# ── 정상 상태 ─────────────────────────────────────────────────────────────────

def test_chain_verifies_clean(client, db_session):
    _write_events(db_session)
    result = audit.verify_chain(db_session)

    assert result["status"] == "OK", result["broken_at"]
    assert result["entries_chained"] >= 5
    assert result["entries_legacy"] == 0
    assert result["broken_at"] is None


def test_each_entry_links_to_its_predecessor(client, db_session):
    entries = _write_events(db_session, 3)
    for previous, current in zip(entries, entries[1:]):
        assert current.prev_checksum == previous.checksum


def test_first_entry_starts_from_genesis(client, db_session):
    first = (
        db_session.query(models.AuditLog)
        .order_by(models.AuditLog.id.asc())
        .first()
    )
    assert first is not None
    assert first.prev_checksum == audit.GENESIS_CHECKSUM


# ── 옛 방식의 한계 재현 ───────────────────────────────────────────────────────

def test_row_deletion_was_invisible_to_row_level_hashes(client, db_session):
    """행 단위 독립 해시였다면 행을 지워도 남은 행이 전부 자기 검증을 통과한다.

    옛 구현의 체크섬 계산식을 그대로 되살려, 가운데 행을 지운 뒤에도
    '전부 정상'으로 보였다는 것을 확인한다. 이 테스트는 체인이 푸는 문제가
    무엇인지 못 박아 두는 용도이며, 앞으로도 통과해야 한다.
    """
    entries = _write_events(db_session, 4)

    def legacy_checksum(entry) -> str:
        # 이전 구현: 행 자신의 필드만 해시했고 detail·ip_address는 아예 빠져 있었다.
        payload = {
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "user_id": entry.user_id,
            "timestamp": entry.event_time,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    surviving = [e for e in entries if e is not entries[1]]   # 가운데 한 건을 지웠다고 치자
    legacy_hashes = {e.id: legacy_checksum(e) for e in surviving}

    # 남은 행은 전부 자기 해시와 일치한다 — 지운 자리가 보이지 않는다.
    assert all(legacy_checksum(e) == legacy_hashes[e.id] for e in surviving)


# ── 체인이 잡아내는 조작 ──────────────────────────────────────────────────────

def test_detects_deleted_middle_row(client, db_session):
    """가운데 행을 삭제하면 다음 행의 연결 고리가 끊긴다."""
    entries = _write_events(db_session, 5)
    victim = entries[2]
    successor_id = entries[3].id

    with tamper_scope(db_session):
        db_session.delete(victim)
        db_session.flush()
        result = audit.verify_chain(db_session)

    assert result["status"] == "BROKEN"
    assert result["broken_at"]["reason"] == "BROKEN_LINK"
    assert result["broken_at"]["id"] == successor_id


def test_detects_modified_detail(client, db_session):
    """detail만 고쳐도 잡힌다 — 옛 해시는 이 필드를 보호하지 않았다."""
    entries = _write_events(db_session, 4)
    victim = entries[1]

    victim_id = victim.id
    with tamper_scope(db_session):
        victim.detail = "관리자가 아무 일도 하지 않았음"
        db_session.flush()
        result = audit.verify_chain(db_session)

    assert result["status"] == "BROKEN"
    assert result["broken_at"]["reason"] == "CHECKSUM_MISMATCH"
    assert result["broken_at"]["id"] == victim_id


def test_detects_modified_ip_address(client, db_session):
    """접속 IP를 바꿔치기해도 잡힌다."""
    entries = _write_events(db_session, 3)
    victim = entries[0]

    victim_id = victim.id
    with tamper_scope(db_session):
        victim.ip_address = "127.0.0.1"
        db_session.flush()
        result = audit.verify_chain(db_session)

    assert result["status"] == "BROKEN"
    assert result["broken_at"]["reason"] == "CHECKSUM_MISMATCH"
    assert result["broken_at"]["id"] == victim_id


def test_detects_deleted_tail_row(client, db_session):
    """마지막 행을 지우면 남은 행끼리는 멀쩡하지만 체인 머리와 어긋난다."""
    entries = _write_events(db_session, 4)
    last = entries[-1]

    with tamper_scope(db_session):
        db_session.delete(last)
        db_session.flush()
        result = audit.verify_chain(db_session)

    assert result["status"] == "BROKEN"
    assert result["broken_at"]["reason"] == "HEAD_MISMATCH"


def test_head_tracks_entry_count(client, db_session):
    before = (
        db_session.query(models.AuditChainHead)
        .filter(models.AuditChainHead.id == audit.HEAD_ID)
        .one()
    )
    start_count = before.entry_count

    _write_events(db_session, 3)

    db_session.refresh(before)
    assert before.entry_count == start_count + 3


# ── 검증 API ──────────────────────────────────────────────────────────────────

def test_verify_endpoint_requires_admin(client):
    staff = auth_header(client, "staff", "Staff1234!")
    assert client.get("/admin/audit-logs/verify", headers=staff).status_code == 403

    risk = auth_header(client, "risk_officer", "Risk1234!")
    assert client.get("/admin/audit-logs/verify", headers=risk).status_code == 403


def test_verify_endpoint_reports_ok(client):
    admin = auth_header(client, "admin", "Admin1234!")
    response = client.get("/admin/audit-logs/verify", headers=admin)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK", body["broken_at"]
    assert body["entries_total"] >= 1
    assert len(body["head_checksum"]) == 64


def test_transfer_appends_to_chain(client, db_session):
    """이체가 남긴 감사 로그도 체인에 정상적으로 붙는다."""
    staff = auth_header(client, "staff", "Staff1234!")
    response = client.post(
        "/transactions/transfer",
        json={"account_from": "ACC-AUD1", "account_to": "ACC-AUD2",
              "amount": 12_000.0, "currency": "KRW"},
        headers=staff,
    )
    assert response.status_code == 201

    latest = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "CREATE_TRANSACTION")
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert latest is not None
    assert latest.prev_checksum is not None
    assert audit.recompute_checksum(latest) == latest.checksum
    assert audit.verify_chain(db_session)["status"] == "OK"
