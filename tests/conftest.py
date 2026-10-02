"""
테스트 픽스처 설정.

scope="module": 테스트 파일(모듈)별로 독립된 SQLite DB를 사용한다.
- 모듈 시작 시 테이블을 초기화(drop→create)하여 다른 모듈의 데이터가 유입되지 않도록 격리한다.
- 동일 모듈 내 테스트들은 DB를 공유하며 데이터가 누적된다 (의도적 설계).
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db import Base, get_db
from app.fds_engine import seed_default_rules
from app.security import get_password_hash
from app import audit, models

# ── 테스트 DB 선택 ────────────────────────────────────────────────────────────
#
# 기본은 SQLite 파일 DB라 Docker 없이 돈다. FDS_TEST_DB=mysql이면 Testcontainers로 운영과 같은
# MySQL 8.0을 띄워 **같은 테스트를 그대로** 그 위에서 돌린다 — SQLite에서는 흉내만 낸 FOR UPDATE 행 잠금,
# 유니크 제약 경합, 데드락을 실제 엔진으로 검증하기 위해서다.
#
#     FDS_TEST_DB=mysql pytest tests/test_concurrency.py tests/test_idempotency.py tests/test_mysql_locking.py
#
# MySQL에서만 의미가 있는 테스트(데드락 재현, 잠금 대기 초과)는 @pytest.mark.mysql로 표시하고,
# 기본 실행에서는 건너뛴다.
TEST_DB = os.environ.get("FDS_TEST_DB", "sqlite")

if TEST_DB == "mysql":
    import atexit

    from testcontainers.mysql import MySqlContainer

    from app.config import settings
    from app.db import configure_lock_wait_timeout

    _mysql = MySqlContainer("mysql:8.0", dialect="pymysql")
    _mysql.start()
    atexit.register(_mysql.stop)
    engine = create_engine(_mysql.get_connection_url(), pool_size=20, max_overflow=10, pool_pre_ping=True)
    configure_lock_wait_timeout(engine, settings.mysql_lock_wait_timeout_seconds)
else:
    engine = create_engine("sqlite:///./test.db", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def pytest_configure(config):
    config.addinivalue_line("markers", "mysql: 실제 MySQL이 필요한 테스트 (FDS_TEST_DB=mysql일 때만 실행)")


def pytest_collection_modifyitems(config, items):
    if TEST_DB == "mysql":
        return
    skip = pytest.mark.skip(reason="FDS_TEST_DB=mysql 일 때만 실행 (Docker 필요)")
    for item in items:
        if "mysql" in item.keywords:
            item.add_marker(skip)


# ── SQLite에서 쓰기 트랜잭션 직렬화 ────────────────────────────────────────────
#
# 운영 DB는 MySQL이고, 이체는 `SELECT ... FOR UPDATE`로 계좌 행을 잠근다.
# SQLAlchemy의 SQLite 방언은 이 구문을 **조용히 버린다** — SQLite에는 행 단위
# 잠금이 없기 때문이다. 그래서 아무것도 하지 않으면 SQLite 테스트에서는
# 잠금이 걸리지 않은 상태로 동시성 테스트가 돌아 "통과했지만 아무것도
# 증명하지 못한" 테스트가 된다.
#
# SQLite에는 대신 DB 단위 쓰기 잠금이 있다. 문제는 pysqlite가 기본적으로
# 트랜잭션을 지연 시작(BEGIN DEFERRED)해서, 읽기 시점에는 잠금을 잡지 않고
# 첫 쓰기에서야 잡는다는 점이다. 이체는 "읽고 → 판단하고 → 쓰는" 연산이므로
# 그 사이에 다른 트랜잭션이 끼어들 수 있다 — FOR UPDATE가 없는 것과 같은 상황.
#
# BEGIN IMMEDIATE로 트랜잭션 시작 시점에 쓰기 잠금을 잡게 하면 읽기-판단-쓰기가
# 하나로 묶인다. MySQL의 행 잠금과 잠금 범위는 다르지만(DB 전체 vs 행),
# "동시 이체가 같은 잔액을 두 번 읽을 수 없다"는 성질은 같다.
#
# 정리: MySQL에서는 FOR UPDATE가, SQLite에서는 BEGIN IMMEDIATE가 직렬화를 맡는다.
# FOR UPDATE 구문이 실제로 SQL에 실린다는 것 자체는
# tests/test_concurrency.py::test_lock_query_emits_for_update 가 따로 검증한다.

# FDS_TEST_NO_LOCK=1 로 이 직렬화를 꺼서 "잠금이 없으면 정말 깨지는가"를
# 확인할 수 있다(음성 대조군). tests/test_concurrency.py 상단 주석 참고.
# MySQL에서는 FOR UPDATE가 실제로 잠그므로 이 우회가 필요 없다.
SERIALIZE_WRITES = TEST_DB == "sqlite" and os.environ.get("FDS_TEST_NO_LOCK") != "1"


@event.listens_for(engine, "connect")
def _sqlite_connect(dbapi_connection, connection_record):
    if not SERIALIZE_WRITES:
        return
    # pysqlite의 암묵적 트랜잭션 관리를 끈다 — 아래 BEGIN을 직접 내기 위해서다.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 10000")  # 잠금 대기 10초 (기본값 0 = 즉시 실패)
    cursor.close()


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(connection):
    if not SERIALIZE_WRITES:
        return
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="module")
def client():
    # 모듈마다 깨끗한 DB에서 시작 (이전 모듈의 데이터 차단)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    seed_default_rules(db)
    audit.ensure_chain_head(db)
    db.add_all([
        models.User(
            username="admin",
            email="admin@test.local",
            hashed_password=get_password_hash("Admin1234!"),
            role="ADMIN",
        ),
        models.User(
            username="risk_officer",
            email="risk@test.local",
            hashed_password=get_password_hash("Risk1234!"),
            role="RISK_OFFICER",
        ),
        models.User(
            username="staff",
            email="staff@test.local",
            hashed_password=get_password_hash("Staff1234!"),
            role="STAFF",
        ),
    ])
    db.commit()
    db.close()

    with patch("app.main._initialize_db"):
        with TestClient(app) as c:
            yield c


def auth_header(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/auth/token", data={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin_auth(client):
    return auth_header(client, "admin", "Admin1234!")


@pytest.fixture(scope="module")
def risk_auth(client):
    return auth_header(client, "risk_officer", "Risk1234!")


@pytest.fixture(scope="module")
def staff_auth(client):
    return auth_header(client, "staff", "Staff1234!")


@pytest.fixture
def db_session():
    """API를 거치지 않고 DB를 직접 건드리는 테스트용 세션.

    감사 로그 위변조 탐지 테스트처럼 "정상 경로로는 불가능한 조작"을 흉내낼 때 쓴다.
    """
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()
