import os
import random
import time
from datetime import datetime

import requests
from requests.exceptions import RequestException

API_BASE_URL = os.environ.get("API_BASE_URL", "http://transaction-api:8000")
INTERVAL_SECONDS = int(os.environ.get("SIMULATOR_INTERVAL", "12"))
HIGH_VALUE_THRESHOLD = int(os.environ.get("SIMULATOR_HIGH_VALUE_THRESHOLD", "100000"))
LOGIN_USER = os.environ.get("SIMULATOR_LOGIN_USER", "simuser")

TRANSFER_URL = f"{API_BASE_URL}/transactions/transfer"
LOGIN_URL = f"{API_BASE_URL}/auth/login"
HEALTH_URL = f"{API_BASE_URL}/health"

NORMAL_AMOUNTS = [1000, 5000, 12000, 30000, 45000]
HIGH_AMOUNTS = [HIGH_VALUE_THRESHOLD + 10000, HIGH_VALUE_THRESHOLD + 50000, HIGH_VALUE_THRESHOLD + 100000]


def wait_for_api(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(HEALTH_URL, timeout=5)
            if resp.status_code == 200:
                print("API ready")
                return True
        except RequestException:
            pass
        print("Waiting for API to become ready...")
        time.sleep(3)
    return False


def send_transaction(amount, currency="KRW"):
    payload = {
        "account_from": f"ACC-{random.randint(1000, 9999)}",
        "account_to": f"ACC-{random.randint(1000, 9999)}",
        "amount": amount,
        "currency": currency,
    }
    try:
        resp = requests.post(TRANSFER_URL, json=payload, timeout=10)
        print(f"transfer {amount} -> {resp.status_code}", resp.text)
    except RequestException as exc:
        print("transaction error", exc)


def send_login_failure(username):
    payload = {"username": username, "password": "wrong-password"}
    try:
        resp = requests.post(LOGIN_URL, json=payload, timeout=10)
        print(f"login fail {username} -> {resp.status_code}", resp.text)
    except RequestException as exc:
        print("login error", exc)


def run_cycle():
    print(f"[{datetime.utcnow().isoformat()}] Simulator cycle start")

    for amount in random.sample(NORMAL_AMOUNTS, 3):
        send_transaction(amount)
        time.sleep(1)

    send_transaction(random.choice(HIGH_AMOUNTS))
    time.sleep(1)

    if random.random() < 0.35:
        send_transaction(random.choice(HIGH_AMOUNTS))
        time.sleep(1)

    for _ in range(4):
        send_login_failure(LOGIN_USER)
        time.sleep(1)

    print(f"[{datetime.utcnow().isoformat()}] Simulator cycle complete")


def main():
    if not wait_for_api():
        print("API did not become ready in time. Exiting.")
        return

    while True:
        run_cycle()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
