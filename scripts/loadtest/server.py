#!/usr/bin/env python
"""부하 테스트용 API 서버 — 구성(variant)에 따라 경로 일부를 끈 채로 uvicorn을 띄운다.

    python scripts/loadtest/server.py --variant baseline|no-audit|no-scoring --port 8100

운영 코드에 "감사 끄기" 같은 스위치를 넣지 않으려고, 여기서 앱을 import한 뒤 해당 함수만 바꿔 끼운다.
  baseline    운영과 같다
  no-audit    감사 로그 기록(app.audit.log_event)을 no-op으로 — audit_chain_head 단일 행 FOR UPDATE의 비용
  no-scoring  FDS 룰 채점(evaluate_transaction)을 0점으로 — 채점 쿼리 3개(velocity, 수취인 입금 수,
              최근 50건 실패율)의 비용. 거래 전 잔액 조회와 룰 목록 조회는 남는다.
DB 접속은 운영과 같은 설정(MYSQL_HOST 등 환경 변수)을 쓴다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn

from app import audit
from app.main import app
from app.routers import transactions

VARIANTS = ("baseline", "no-audit", "no-scoring")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=VARIANTS, default="baseline")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    if args.variant == "no-audit":
        audit.log_event = lambda *a, **k: None
    elif args.variant == "no-scoring":
        transactions.evaluate_transaction = lambda *a, **k: (0.0, [], [])

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
