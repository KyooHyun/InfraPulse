"""
테스트 픽스처 설정.
MySQL 대신 SQLite 인메모리 DB를 사용하고,
FastAPI 앱의 get_db 의존성을 교체하여 실제 DB에 영향을 주지 않는다.
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


@pytest.fixture(scope="session")
def client():
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

    # MySQL 초기화 로직을 건너뛴다 (테스트 DB는 이미 위에서 준비됨)
    with patch("app.main._initialize_db"):
        with TestClient(app) as c:
            yield c

    Base.metadata.drop_all(bind=engine)


def auth_header(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/auth/token", data={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def admin_auth(client):
    return auth_header(client, "admin", "Admin1234!")


@pytest.fixture(scope="session")
def risk_auth(client):
    return auth_header(client, "risk_officer", "Risk1234!")


@pytest.fixture(scope="session")
def staff_auth(client):
    return auth_header(client, "staff", "Staff1234!")
