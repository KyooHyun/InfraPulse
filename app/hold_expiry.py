"""보류 만료 작업자 — 보류 시간(FDS_HOLD_TTL_HOURS)이 지난 이체를 취소해 출금 계좌로 돌려준다.

    python -m app.hold_expiry                  # 한 번 실행 (--hours로 보류 시간 지정)
    python -m app.hold_expiry --every 300      # 300초마다 반복 (docker-compose의 hold-expirer 서비스)

대사(scripts/reconcile.py)는 사람이 필요할 때 돌려도 되지만, 만료는 아무도 돌리지 않으면 보류된 돈이 무기한
묶인다. 그래서 운영 구성(docker-compose.yml)에 이 작업자를 상시 서비스로 둔다. 여러 개를 띄워도 안전하다 —
같은 보류를 두 작업자가 동시에 만료시켜도 holds.resolve()의 상태 조건 UPDATE가 하나만 통과시킨다.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import timedelta
from typing import List, Optional

from .config import settings
from .db import SessionLocal
from .holds import expire_holds

logger = logging.getLogger("hold_expiry")


def run_once(ttl: timedelta) -> List[int]:
    db = SessionLocal()
    try:
        return expire_holds(db, ttl)
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="보류 시간이 지난 이체를 취소한다")
    parser.add_argument("--hours", type=float, default=settings.fds_hold_ttl_hours, help="보류 시간 (기본 FDS_HOLD_TTL_HOURS)")
    parser.add_argument("--every", type=float, default=None, help="이 간격(초)으로 반복. 없으면 한 번만 실행")
    args = parser.parse_args(argv)
    ttl = timedelta(hours=args.hours)

    while True:
        try:
            expired = run_once(ttl)
            if expired or args.every is None:
                logger.info("만료 취소 %d건 %s", len(expired), expired if expired else "")
        except Exception:
            if args.every is None:
                raise
            logger.exception("만료 처리 실패 — 다음 주기에 다시 시도한다")   # DB 일시 장애로 작업자가 죽지 않게
        if args.every is None:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
