"""운영 경로와 오프라인 평가 경로가 같은 점수를 내는지 고정한다.

점수 함수(evaluate_signals, feature_values)는 운영과 평가가 공유한다. 하지만 그 입력을
모으는 쪽은 둘이다 — 운영은 거래마다 DB를 조회하고(collect_signals, extract_features),
평가는 277만 건을 DB 조회 없이 한 번에 계산한다(calibration/dataset.py). 둘이 갈라지면
"평가 스크립트가 엔진을 따로 구현했다"는 문제가 한 단계 아래에서 반복된다.

그래서 같은 거래 이력을 양쪽에 넣고 신호·룰 점수·ML 피처가 모두 일치하는지 본다.
이력은 일부러 경계가 걸리게 만든다 — 같은 시각 거래(동률), 윈도우 경계에 정확히
걸리는 간격(10분), 실패율 윈도우(50건)를 넘는 길이.
"""
import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import models
from app.fds_engine import collect_signals, evaluate_signals
from app.ml.features import extract_features
from calibration.dataset import load_from_db, ml_feature_matrix
from calibration.rules import default_rules
from tests.conftest import TestingSession

N_TRANSACTIONS = 300
ACCOUNTS = [f"PAR-{i:02d}" for i in range(6)]
RECIPIENTS = [f"PAR-R{i:02d}" for i in range(15)]
# 0분 간격(동률), 정확히 10분(VELOCITY 윈도우 경계), 정확히 24시간(NEW_RECIPIENT 윈도우 경계)을 섞는다
GAPS_MINUTES = [0, 0, 1, 2, 3, 5, 10, 10, 11, 30, 24 * 60]


@pytest.fixture(scope="module")
def replayed(client):
    """거래를 시간 순으로 하나씩 기록하며, 기록 직전에 운영 경로로 신호를 모은다.

    운영 이체도 같은 순서다 — 신호 수집(기록 전) → 거래 기록.
    모듈에서 한 번만 재생한다 — 테스트마다 다시 쌓으면 이력이 겹친다.
    """
    db_session = TestingSession()
    rng = random.Random(20261003)
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    live_signals = []
    for _ in range(N_TRANSACTIONS):
        now += timedelta(minutes=rng.choice(GAPS_MINUTES))
        account = rng.choice(ACCOUNTS)
        recipient = rng.choice(RECIPIENTS)
        amount = rng.choice([5_000.0, 50_000.0, 100_000.0, 2_000_000.0])
        # 잔액 0(BALANCE_DRAIN·DEST_EMPTY 경계), 금액과 같은 잔액(정확히 비우기)을 섞는다
        before = rng.choice([0.0, amount, amount / 0.95, rng.uniform(0, 5_000_000)])
        dest_before = rng.choice([0.0, rng.uniform(0, 5_000_000)])

        live_signals.append(collect_signals(
            amount, account, db_session, now=now,
            account_to=recipient, balance_orig_before=before, balance_dest_before=dest_before,
        ))

        db_session.add(models.Transaction(
            account_from=account,
            account_to=recipient,
            amount=amount,
            currency="KRW",
            status="failed" if rng.random() < 0.3 else "success",
            reason="parity",
            created_at=now,
            balance_orig_before=before,
            balance_orig_after=max(before - amount, 0.0),
            balance_dest_before=dest_before,
            balance_dest_after=rng.uniform(0, 5_000_000),
            is_fraud=rng.random() < 0.1,
        ))
        db_session.commit()

    transactions = (
        db_session.query(models.Transaction)
        .filter(models.Transaction.reason == "parity")
        .order_by(models.Transaction.created_at.asc(), models.Transaction.id.asc())
        .all()
    )
    # 운영 경로의 ML 피처 — 거래가 기록된 뒤 계산한다(운영 이체와 같은 순서).
    # 이력 전체가 기록된 뒤에 계산하지만, extract_features는 자신보다 먼저 기록된 거래만 센다.
    live_features = np.array([extract_features(tx, db_session) for tx in transactions])
    offline = load_from_db(db_session)
    db_session.commit()
    yield live_signals, live_features, offline
    db_session.close()


def test_history_exercises_the_edges(replayed):
    """테스트 이력이 실제로 경계 조건을 밟는지 — 안 밟으면 아래 일치는 아무것도 증명하지 못한다."""
    live_signals, _, _ = replayed
    velocities = [s["VELOCITY"] for s in live_signals]
    assert max(velocities) >= 3, "VELOCITY가 누적되는 구간이 있어야 한다"
    assert any(s["FAILURE_RATE"] is None for s in live_signals), "실패율 표본 부족 구간"
    assert any(s["FAILURE_RATE"] is not None for s in live_signals), "실패율 계산 구간"
    for rule in ("BALANCE_DRAIN", "DEST_EMPTY", "NEW_RECIPIENT"):
        values = {s[rule] >= (0.9 if rule == "BALANCE_DRAIN" else 1.0) for s in live_signals}
        assert values == {True, False}, f"{rule}가 발화하는 거래와 안 하는 거래가 모두 있어야 한다"


def test_rule_signals_match(replayed):
    live_signals, _, offline = replayed
    assert len(offline) == N_TRANSACTIONS
    for i, (live, sample) in enumerate(zip(live_signals, offline)):
        assert live == sample.signals, f"{i}번째 거래의 룰 신호가 다르다"


def test_rule_scores_match(replayed):
    live_signals, _, offline = replayed
    rules = default_rules()
    for live, sample in zip(live_signals, offline):
        assert evaluate_signals(live, rules) == evaluate_signals(sample.signals, rules)


def test_ml_features_match(replayed):
    _, live_features, offline = replayed
    np.testing.assert_allclose(ml_feature_matrix(offline), live_features)
