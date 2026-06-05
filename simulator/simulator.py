import os
import random
import time
from datetime import datetime, timezone

import requests
from requests.exceptions import RequestException

API_BASE_URL = os.environ.get("API_BASE_URL", "http://transaction-api:8000")
INTERVAL_SECONDS = int(os.environ.get("SIMULATOR_INTERVAL", "15"))
HIGH_VALUE_THRESHOLD = int(os.environ.get("SIMULATOR_HIGH_VALUE_THRESHOLD", "100000"))
SIMULATOR_USER = os.environ.get("SIMULATOR_USER", "admin")
SIMULATOR_PASSWORD = os.environ.get("SIMULATOR_PASSWORD", "Admin1234!")
LOGIN_FAIL_USER = os.environ.get("SIMULATOR_LOGIN_FAIL_USER", "simuser")

TRANSFER_URL = f"{API_BASE_URL}/transactions/transfer"
TOKEN_URL = f"{API_BASE_URL}/auth/token"
HEALTH_URL = f"{API_BASE_URL}/health"

NORMAL_AMOUNTS = [1000, 5000, 12000, 30000, 45000]
HIGH_AMOUNTS = [
    HIGH_VALUE_THRESHOLD + 10_000,
    HIGH_VALUE_THRESHOLD + 50_000,
    HIGH_VALUE_THRESHOLD + 100_000,
]


def wait_for_api(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(HEALTH_URL, timeout=5).status_code == 200:
                print("API ready")
                return True
        except RequestException:
            pass
        print("Waiting for API...")
        time.sleep(3)
    return False


def get_token() -> str | None:
    """admin 계정으로 JWT 토큰을 발급받는다."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={"username": SIMULATOR_USER, "password": SIMULATOR_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]
        print(f"Auth failed: {resp.status_code}")
        return None
    except RequestException as exc:
        print("Auth error:", exc)
        return None


def send_transaction(amount: float, token: str, currency: str = "KRW") -> None:
    payload = {
        "account_from": f"ACC-{random.randint(1000, 9999)}",
        "account_to": f"ACC-{random.randint(1000, 9999)}",
        "amount": amount,
        "currency": currency,
    }
    try:
        resp = requests.post(
            TRANSFER_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        risk = resp.json().get("risk_score", "?") if resp.status_code == 201 else "-"
        print(f"transfer {amount:>10,.0f} KRW -> {resp.status_code} (risk_score={risk})")
    except RequestException as exc:
        print("transaction error:", exc)


def send_login_failure(username: str) -> None:
    """로그인 실패 이상징후 시뮬레이션."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={"username": username, "password": "wrong-password"},
            timeout=10,
        )
        print(f"login fail [{username}] -> {resp.status_code}")
    except RequestException as exc:
        print("login error:", exc)


def run_cycle(token: str) -> None:
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] ── cycle start ──")

    # 정상 거래 3건
    for amount in random.sample(NORMAL_AMOUNTS, 3):
        send_transaction(amount, token)
        time.sleep(1)

    # 고액 거래 1건 (항상)
    send_transaction(random.choice(HIGH_AMOUNTS), token)
    time.sleep(1)

    # 고액 거래 추가 1건 (35% 확률) — 실패율 누적 유도
    if random.random() < 0.35:
        send_transaction(random.choice(HIGH_AMOUNTS), token)
        time.sleep(1)

    # 로그인 실패 4회 — LOGIN_FAILURE 이상징후 트리거
    for _ in range(4):
        send_login_failure(LOGIN_FAIL_USER)
        time.sleep(1)

    print(f"[{datetime.now(timezone.utc).isoformat()}] ── cycle complete ──")


def main() -> None:
    if not wait_for_api():
        print("API did not become ready. Exiting.")
        return

    while True:
        token = get_token()
        if token:
            run_cycle(token)
        else:
            print("Could not obtain auth token — skipping cycle")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
