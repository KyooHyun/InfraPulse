#!/usr/bin/env python
"""이체 부하 테스트 — 동시 작업자 N개가 계좌 풀 사이에서 무작위 이체를 정해진 시간 동안 보낸다.

    python scripts/loadtest/run.py --url http://127.0.0.1:8100 --concurrency 20 --duration 30 --label baseline

첫 --warmup초는 집계에서 뺀다(계좌 개설, 커넥션 풀 준비). 결과는 한 줄 JSON으로 출력한다.
처리량은 201 응답 기준이고, 지연은 클라이언트에서 잰 요청 왕복 시간이다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time

import httpx


def _pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else float("nan")


async def _worker(client, headers, accounts, start, warm_until, deadline, out, rng):
    while (now := time.perf_counter()) < deadline:
        sender, receiver = rng.sample(accounts, 2)
        t0 = time.perf_counter()
        try:
            resp = await client.post(
                "/transactions/transfer",
                json={"account_from": sender, "account_to": receiver,
                      "amount": rng.choice([1_000, 5_000, 12_000, 30_000]), "currency": "KRW"},
                headers=headers,
            )
            ok = resp.status_code == 201
        except httpx.HTTPError:
            ok = False
        elapsed = time.perf_counter() - t0
        if t0 >= warm_until:
            out["latencies" if ok else "errors"].append(elapsed)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8100")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--accounts", type=int, default=200)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    accounts = [f"LT-{i:04d}" for i in range(args.accounts)]
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=60.0, limits=limits) as client:
        token = (await client.post("/auth/token", data={"username": "staff", "password": "Staff1234!"})).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        out = {"latencies": [], "errors": []}
        start = time.perf_counter()
        warm_until = start + args.warmup
        deadline = warm_until + args.duration
        rngs = [random.Random(i) for i in range(args.concurrency)]
        await asyncio.gather(*(
            _worker(client, headers, accounts, start, warm_until, deadline, out, rngs[i])
            for i in range(args.concurrency)
        ))

    lat = out["latencies"]
    print(json.dumps({
        "label": args.label,
        "concurrency": args.concurrency,
        "duration_s": args.duration,
        "requests": len(lat),
        "errors": len(out["errors"]),
        "throughput_rps": round(len(lat) / args.duration, 1),
        "p50_ms": round(statistics.median(lat) * 1000, 1) if lat else None,
        "p95_ms": round(_pct(lat, 0.95) * 1000, 1),
        "p99_ms": round(_pct(lat, 0.99) * 1000, 1),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
