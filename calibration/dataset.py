"""라벨 붙은 거래 → (정답, 룰 신호) 쌍.

캘리브레이션 분석은 전부 이 형태를 입력으로 쓴다. 중요한 건 여기서 만든 신호를
**운영과 같은 함수**(app.fds_engine.evaluate_signals)에 넣는다는 점이다.
분석용으로 룰을 다시 구현하면 "분석에서 근거를 확인한 룰"과 "운영에서 도는 룰"이
조용히 갈라지고, 그러면 근거는 있지만 그 근거가 운영 시스템의 것이 아니게 된다.

출처는 셋이고, 각각 만들어낼 수 있는 신호가 다르다. 없는 신호는 None으로 두며
evaluate_signals가 그 룰을 판정하지 않는다 — 0으로 채우면 "발화하지 않았다"는
관측으로 둔갑해 lift가 왜곡된다.

  db          transactions 테이블 중 is_fraud가 채워진 행 (scripts/load_paysim.py 적재분)
              → HIGH_VALUE, VELOCITY, FAILURE_RATE

  paysim      PaySim CSV                → HIGH_VALUE, VELOCITY
              실패/성공 상태가 없어 FAILURE_RATE는 만들 수 없다.

  creditcard  ULB Credit Card Fraud CSV → HIGH_VALUE
              계좌 식별자가 없어 VELOCITY를 만들 수 없다. 금액 임계값(10만원 자리)
              하나만 보고 싶을 때 쓴다.

ML 피처도 같은 표본에서 만든다(ml_feature_matrix). 각 표본은 피처의 원천값(잔액,
시각)만 들고 있고, 피처 계산은 운영과 같은 app.ml.features.feature_values가 한다.

의존성은 표준 라이브러리만 쓴다. 캘리브레이션은 판단 근거를 만드는 코드이므로
"pandas가 없어서 못 돌렸다"는 이유로 건너뛰게 되면 안 된다. (ML 피처 행렬만 numpy가
필요하며, 그 함수 안에서만 import한다.)
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fds_engine import FAILURE_RATE_WINDOW, VELOCITY_WINDOW_MINUTES

# PaySim의 step은 1시간 단위다. 운영 VELOCITY 윈도우(10분)와 다르므로, PaySim에서
# 계산한 velocity는 같은 계좌의 "1시간 내 거래 수"다. 실제 룰보다 넓은 창이라
# velocity 값이 과대평가되는 방향이며, 이 점은 결과 해석에 그대로 적는다.
PAYSIM_STEP_MINUTES = 60


@dataclass
class Sample:
    """거래 한 건의 정답과 룰 신호."""

    label: int                                  # 1=사기, 0=정상
    signals: Dict[str, Any] = field(default_factory=dict)
    amount: float = 0.0
    account_from: str = ""
    # ML 피처의 원천값 — (orig 전, orig 후, dest 전, dest 후). 출처에 없으면 None.
    balances: Optional[Tuple[float, float, float, float]] = None
    hour: Optional[int] = None
    step: Optional[int] = None   # PaySim 시간 단위(1시간) — 시간 분할 평가용

    @property
    def available_signals(self) -> List[str]:
        return [name for name, value in self.signals.items() if value is not None]


def _velocity_by_window(
    rows: List[Dict[str, Any]],
    account_key: str,
    time_key: str,
    window: float,
) -> List[float]:
    """행 순서대로 훑으며 같은 계좌의 window 내 거래 수(현재 건 포함)를 센다.

    운영 룰과 같은 정의로 맞춘다 — 과거만 본다. 전체 데이터를 보고 세면 미래
    정보가 들어가(look-ahead) lift가 실제보다 좋게 나온다. 캘리브레이션에서
    이 실수를 하면 "데이터로 검증했다"는 말이 근거가 아니라 착시가 된다.
    """
    history: Dict[str, deque] = defaultdict(deque)
    out: List[float] = []
    for row in rows:
        account = row[account_key]
        now = float(row[time_key])
        seen = history[account]
        while seen and now - seen[0] > window:
            seen.popleft()
        seen.append(now)
        out.append(float(len(seen)))
    return out


def _rolling_failure_rate(statuses: Iterable[str]) -> List[Optional[float]]:
    """직전 FAILURE_RATE_WINDOW건의 실패율. 표본이 모자라면 None."""
    window: deque = deque(maxlen=FAILURE_RATE_WINDOW)
    out: List[Optional[float]] = []
    for status in statuses:
        if len(window) >= FAILURE_RATE_WINDOW:
            out.append(round(sum(window) / len(window), 4))
        else:
            out.append(None)
        window.append(1 if status == "failed" else 0)
    return out


# ── 출처별 로더 ───────────────────────────────────────────────────────────────

def load_from_db(db, limit: Optional[int] = None) -> List[Sample]:
    """transactions 테이블에서 is_fraud가 채워진 행을 읽는다."""
    from app import models

    query = (
        db.query(models.Transaction)
        .filter(models.Transaction.is_fraud.isnot(None))
        .order_by(models.Transaction.created_at.asc(), models.Transaction.id.asc())
    )
    if limit:
        query = query.limit(limit)
    transactions = query.all()

    rows = [
        {
            "account": tx.account_from,
            "minutes": tx.created_at.timestamp() / 60.0 if tx.created_at else 0.0,
            "amount": float(tx.amount),
            "status": tx.status,
            "label": int(bool(tx.is_fraud)),
            "balances": (tx.balance_orig_before, tx.balance_orig_after,
                         tx.balance_dest_before, tx.balance_dest_after),
            "hour": tx.created_at.hour if tx.created_at else None,
        }
        for tx in transactions
    ]
    velocities = _velocity_by_window(rows, "account", "minutes", VELOCITY_WINDOW_MINUTES)
    failure_rates = _rolling_failure_rate(row["status"] for row in rows)

    return [
        Sample(
            label=row["label"],
            amount=row["amount"],
            account_from=row["account"],
            balances=row["balances"],
            hour=row["hour"],
            signals={
                "HIGH_VALUE": row["amount"],
                "FAILURE_RATE": failure_rate,
                "VELOCITY": velocity,
            },
        )
        for row, velocity, failure_rate in zip(rows, velocities, failure_rates)
    ]


def load_from_paysim(path: Path, limit: Optional[int] = None) -> List[Sample]:
    """PaySim CSV. TRANSFER/CASH_OUT만 읽는다 — 사기 라벨이 이 두 유형에만 있다."""
    fraud_types = {"TRANSFER", "CASH_OUT"}
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as fp:
        for record in csv.DictReader(fp):
            if record["type"] not in fraud_types:
                continue
            step = int(record["step"])
            rows.append({
                "account": record["nameOrig"],
                "minutes": float(step) * PAYSIM_STEP_MINUTES,
                "step": step,
                "amount": float(record["amount"]),
                "label": int(float(record["isFraud"])),
                "balances": (float(record["oldbalanceOrg"]), float(record["newbalanceOrig"]),
                             float(record["oldbalanceDest"]), float(record["newbalanceDest"])),
            })
            if limit and len(rows) >= limit:
                break

    velocities = _velocity_by_window(rows, "account", "minutes", PAYSIM_STEP_MINUTES)
    return [
        Sample(
            label=row["label"],
            amount=row["amount"],
            account_from=row["account"],
            balances=row["balances"],
            # scripts/load_paysim.py가 step을 2024-01-01 00:00 UTC + step시간으로 적재하므로
            # DB에 적재한 뒤 created_at.hour로 구한 값과 같다.
            hour=row["step"] % 24,
            step=row["step"],
            signals={
                "HIGH_VALUE": row["amount"],
                "FAILURE_RATE": None,   # PaySim에는 거래 성공/실패 상태가 없다
                "VELOCITY": velocity,
            },
        )
        for row, velocity in zip(rows, velocities)
    ]


def load_from_creditcard(path: Path, limit: Optional[int] = None) -> List[Sample]:
    """ULB Credit Card Fraud CSV. 금액 신호 하나만 나온다."""
    samples: List[Sample] = []
    with path.open(encoding="utf-8", newline="") as fp:
        for record in csv.DictReader(fp):
            amount = float(record["Amount"])
            samples.append(
                Sample(
                    label=int(float(record["Class"])),
                    amount=amount,
                    signals={"HIGH_VALUE": amount, "FAILURE_RATE": None, "VELOCITY": None},
                )
            )
            if limit and len(samples) >= limit:
                break
    return samples


def ml_feature_matrix(samples: List[Sample]):
    """표본 → Isolation Forest 입력 행렬. 운영과 같은 feature_values로 만든다.

    ML의 velocity는 "직전 거래 수"(현재 건 제외)라 룰 VELOCITY 신호에서 1을 뺀다.
    PaySim은 step이 1시간 단위라 VELOCITY 윈도우가 운영(10분)보다 넓다(PAYSIM_STEP_MINUTES).
    """
    import numpy as np
    from app.ml.features import FEATURE_NAMES, feature_values

    if any(s.balances is None for s in samples):
        raise ValueError("잔액 정보가 없는 출처(creditcard)로는 ML 피처를 만들 수 없다")

    out = np.empty((len(samples), len(FEATURE_NAMES)), dtype=float)
    for i, s in enumerate(samples):
        velocity = s.signals.get("VELOCITY")
        orig_before, _, dest_before, _ = s.balances   # 거래 후 잔액은 피처에 쓰지 않는다
        out[i] = feature_values(
            s.amount, orig_before, dest_before, s.hour,
            velocity - 1 if velocity is not None else 0.0,
        )
    return out


SOURCES = {"db", "paysim", "creditcard"}


def load_samples(
    source: str,
    csv_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[Sample]:
    if source not in SOURCES:
        raise ValueError(f"source는 {', '.join(sorted(SOURCES))} 중 하나여야 한다: {source}")

    if source == "db":
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            samples = load_from_db(db, limit)
        finally:
            db.close()
        if not samples:
            raise SystemExit(
                "transactions 테이블에 is_fraud가 채워진 행이 없다.\n"
                "  python scripts/load_paysim.py <csv_path> --limit 200000  으로 먼저 적재하라."
            )
        return samples

    if csv_path is None:
        raise ValueError(f"source={source}에는 --csv 경로가 필요하다")
    if not csv_path.exists():
        raise SystemExit(
            f"데이터 파일이 없다: {csv_path}\n"
            "  PaySim:     https://www.kaggle.com/datasets/ealaxi/paysim1\n"
            "  CreditCard: https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud\n"
            "  내려받아 evaluation/data/ 아래에 두면 된다 (이 경로는 .gitignore 대상)."
        )

    loader = load_from_paysim if source == "paysim" else load_from_creditcard
    return loader(csv_path, limit)


def add_source_arguments(parser) -> None:
    """calibration 스크립트들이 공유하는 CLI 인자."""
    parser.add_argument("--source", choices=sorted(SOURCES), default="db",
                        help="라벨 데이터 출처 (기본: db)")
    parser.add_argument("--csv", type=Path, default=None,
                        help="source가 paysim/creditcard일 때의 CSV 경로")
    parser.add_argument("--limit", type=int, default=None,
                        help="읽을 최대 행 수 (기본: 전체)")


def describe(samples: List[Sample]) -> str:
    total = len(samples)
    positives = sum(s.label for s in samples)
    available = sorted({name for s in samples for name in s.available_signals})
    return (
        f"표본 {total:,}건 (사기 {positives:,} / 정상 {total - positives:,}, "
        f"사기율 {positives / total:.3%})  사용 가능한 신호: {', '.join(available)}"
    )
