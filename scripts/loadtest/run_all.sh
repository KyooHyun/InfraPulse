#!/usr/bin/env bash
# 세 구성(baseline / no-audit / no-scoring)을 각각 새 MySQL 8.0 컨테이너에서 잰다.
#   bash scripts/loadtest/run_all.sh [동시 작업자 수] [측정 초]
# 구성마다 DB를 새로 띄우는 이유: 앞 구성이 쌓은 거래가 다음 구성의 채점 쿼리를 느리게 만들면 비교가 안 된다.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONCURRENCY=${1:-20}
DURATION=${2:-30}
PORT=8100
PYTHON=${PYTHON:-python}   # 앱 의존성(app/requirements.txt)과 httpx가 설치된 파이썬
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_USER=finops_user MYSQL_PASSWORD=finops_pass MYSQL_DB=finops

for VARIANT in baseline no-audit no-scoring; do
  docker rm -f fds-loadtest-mysql >/dev/null 2>&1 || true
  docker run -d --name fds-loadtest-mysql -p 3307:3306 \
    -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=finops \
    -e MYSQL_USER=finops_user -e MYSQL_PASSWORD=finops_pass mysql:8.0 >/dev/null
  until docker exec fds-loadtest-mysql mysql -ufinops_user -pfinops_pass -e "select 1" finops >/dev/null 2>&1; do sleep 2; done

  "$PYTHON" scripts/loadtest/server.py --variant "$VARIANT" --port $PORT &
  SERVER=$!
  until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
    kill -0 $SERVER 2>/dev/null || { echo "서버가 시작되지 못했다 ($VARIANT)" >&2; exit 1; }
    sleep 1
  done

  "$PYTHON" scripts/loadtest/run.py --url "http://127.0.0.1:$PORT" --concurrency "$CONCURRENCY" --duration "$DURATION" --label "$VARIANT"
  ROWS=$(docker exec fds-loadtest-mysql mysql -N -ufinops_user -pfinops_pass -e "select count(*) from transactions" finops 2>/dev/null)
  echo "{\"label\": \"$VARIANT\", \"transactions_rows_after\": $ROWS}"

  kill $SERVER; wait $SERVER 2>/dev/null || true
done
docker rm -f fds-loadtest-mysql >/dev/null 2>&1 || true
