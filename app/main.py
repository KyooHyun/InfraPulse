import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import OperationalError

from .config import settings
from .db import Base, engine, SessionLocal
from .fds_engine import seed_default_rules
from . import ml_engine
from .metrics import (
    anomaly_event_total,
    anomaly_latency_total,
    http_request_duration_seconds,
    http_requests_total,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from .security import get_password_hash
from . import models
from .routers import auth, transactions, fds, compliance, kyc, admin

logger = logging.getLogger(__name__)

DB_INIT_MAX_ATTEMPTS = 30
DB_INIT_RETRY_DELAY = 2

# 응답 지연 임계값 — fds_rules의 LATENCY 룰과 연동
LATENCY_THRESHOLD_SECONDS = 1.0


def _seed_users(db) -> None:
    if db.query(models.User).count() > 0:
        return
    db.add_all([
        models.User(
            username="admin",
            email="admin@finops.local",
            hashed_password=get_password_hash("Admin1234!"),
            role="ADMIN",
        ),
        models.User(
            username="risk_officer",
            email="risk@finops.local",
            hashed_password=get_password_hash("Risk1234!"),
            role="RISK_OFFICER",
        ),
        models.User(
            username="staff",
            email="staff@finops.local",
            hashed_password=get_password_hash("Staff1234!"),
            role="STAFF",
        ),
    ])
    db.commit()
    logger.info("Default users seeded")


def _initialize_db() -> None:
    last_err: OperationalError | None = None
    for attempt in range(1, DB_INIT_MAX_ATTEMPTS + 1):
        try:
            Base.metadata.create_all(bind=engine)
            db = SessionLocal()
            try:
                seed_default_rules(db)
                _seed_users(db)
                ml_engine.load_or_train(db)
            finally:
                db.close()
            logger.info("Database initialized")
            return
        except OperationalError as exc:
            last_err = exc
            logger.warning(
                "DB not ready (%d/%d) — retrying in %ds",
                attempt, DB_INIT_MAX_ATTEMPTS, DB_INIT_RETRY_DELAY,
            )
            time.sleep(DB_INIT_RETRY_DELAY)
    raise RuntimeError("Database did not become ready in time") from last_err


@asynccontextmanager
async def lifespan(app: FastAPI):
    _initialize_db()
    yield


app = FastAPI(
    title="금융 거래 이상징후 모니터링 API (FDS)",
    description=(
        "이상금융거래탐지(FDS) · AML/KYC · 컴플라이언스 보고 · 감사추적 기능을 갖춘 "
        "금융 거래 모니터링 시스템\n\n"
        "**기본 계정**\n"
        "- admin / Admin1234! (ADMIN)\n"
        "- risk_officer / Risk1234! (RISK_OFFICER)\n"
        "- staff / Staff1234! (STAFF)"
    ),
    version="2.1.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(transactions.router)
app.include_router(fds.router)
app.include_router(compliance.router)
app.include_router(kyc.router)
app.include_router(admin.router)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start

    http_requests_total.inc()
    http_request_duration_seconds.observe(duration)

    if duration > LATENCY_THRESHOLD_SECONDS:
        anomaly_event_total.inc()
        anomaly_latency_total.inc()

    return response


@app.get("/health", tags=["시스템"])
def health():
    return {"status": "ok"}


@app.get("/metrics", tags=["시스템"])
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
