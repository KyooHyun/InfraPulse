import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from random import random
from typing import Dict, List

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import settings
from .crud import create_transaction, get_transactions
from .db import Base, engine, get_db
from .metrics import (
    anomaly_event_total,
    anomaly_high_value_total,
    anomaly_latency_total,
    anomaly_login_failure_total,
    anomaly_transaction_failure_total,
    http_request_duration_seconds,
    http_requests_total,
    login_failed_total,
    transaction_failed_total,
    transaction_total,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from .schemas import LoginRequest, TransactionOut, TransferRequest


logger = logging.getLogger(__name__)

app = FastAPI(
    title="금융 거래 이상징후 모니터링 API",
    description="가상 금융 거래 API와 Prometheus 메트릭을 제공하는 FastAPI 서비스",
)

recent_transactions: deque[bool] = deque(maxlen=50)
recent_request_durations: deque[float] = deque(maxlen=100)
login_failures: Dict[str, deque[datetime]] = defaultdict(lambda: deque())

FAILURE_RATE_THRESHOLD = 0.3
LATENCY_THRESHOLD_SECONDS = 1.0
LOGIN_FAILURE_THRESHOLD = 3
LOGIN_FAILURE_WINDOW_SECONDS = 300
HIGH_VALUE_THRESHOLD = 100000.0
DB_INIT_MAX_ATTEMPTS = 30
DB_INIT_RETRY_DELAY_SECONDS = 2


def initialize_database_with_retry() -> None:
    last_error: OperationalError | None = None
    for attempt in range(1, DB_INIT_MAX_ATTEMPTS + 1):
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("Database initialized")
            return
        except OperationalError as exc:
            last_error = exc
            logger.warning(
                "Database is not ready yet (%s/%s). Retrying in %s seconds.",
                attempt,
                DB_INIT_MAX_ATTEMPTS,
                DB_INIT_RETRY_DELAY_SECONDS,
            )
            time.sleep(DB_INIT_RETRY_DELAY_SECONDS)

    raise RuntimeError("Database did not become ready in time") from last_error


@app.on_event("startup")
def on_startup() -> None:
    initialize_database_with_retry()


def check_transaction_failure_rate() -> None:
    if len(recent_transactions) < 10:
        return

    failure_rate = sum(1 for success in recent_transactions if not success) / len(recent_transactions)
    if failure_rate >= FAILURE_RATE_THRESHOLD:
        anomaly_event_total.inc()
        anomaly_transaction_failure_total.inc()


@app.middleware("http")
async def prometheus_middleware(request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    http_requests_total.inc()
    http_request_duration_seconds.observe(duration)
    recent_request_durations.append(duration)

    if duration > LATENCY_THRESHOLD_SECONDS:
        anomaly_event_total.inc()
        anomaly_latency_total.inc()

    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    data = generate_latest()
    return PlainTextResponse(data, media_type=CONTENT_TYPE_LATEST)


@app.get("/transactions", response_model=List[TransactionOut])
def list_transactions(db: Session = Depends(get_db)):
    return get_transactions(db)


@app.post("/transactions/transfer", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def transfer(request: TransferRequest, db: Session = Depends(get_db)):
    failure_chance = 0.15
    if request.amount > 50000:
        failure_chance = 0.25

    success = random() >= failure_chance
    status_text = "success" if success else "failed"
    reason = "completed" if success else "random failure"

    transaction = create_transaction(db, request, status_text, reason)
    transaction_total.inc()
    if not success:
        transaction_failed_total.inc()

    recent_transactions.append(success)
    check_transaction_failure_rate()

    if request.amount >= HIGH_VALUE_THRESHOLD:
        anomaly_event_total.inc()
        anomaly_high_value_total.inc()

    return transaction


@app.post("/auth/login")
def login(request: LoginRequest):
    valid_user = request.username == "admin" and request.password == "password"
    if not valid_user:
        login_failed_total.inc()
        now = datetime.utcnow()
        failures = login_failures[request.username]
        failures.append(now)
        cutoff = now - timedelta(seconds=LOGIN_FAILURE_WINDOW_SECONDS)
        while failures and failures[0] < cutoff:
            failures.popleft()

        if len(failures) >= LOGIN_FAILURE_THRESHOLD:
            anomaly_event_total.inc()
            anomaly_login_failure_total.inc()

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login failed")

    return {"message": "login successful"}
