"""
테스트 픽스처 설정.

scope="module": 테스트 파일(모듈)별로 독립된 SQLite DB를 사용한다.
- 모듈 시작 시 테이블을 초기화(drop→create)하여 다른 모듈의 데이터가 유입되지 않도록 격리한다.
- 동일 모듈 내 테스트들은 DB를 공유하며 데이터가 누적된다 (의도적 설계).
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db import Base, get_db
from app.fds_engine import seed_default_rules
from app.security import get_password_hash
from app import models

TEST_DB_URL = "sqlite:///./test.db"

engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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
