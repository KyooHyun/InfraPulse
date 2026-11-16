#!/usr/bin/env bash
# 거래 100만 행이 쌓인 DB에서 채점 인덱스 전후와 보류 경로의 비용을 잰다.
#   bash scripts/loadtest/run_large.sh [반복 횟수] [동시 작업자 수] [측정 초] [과거 거래 행 수]
#
# 컨테이너 하나에 과거 거래를 한 번 채우고, 세 구성을 번갈아 [반복 횟수]만큼 돈다.
#   no-index   채점 인덱스 3개를 지운 상태 (전)
#   index      인덱스를 만든 상태 (후)
#   hold-10pct 인덱스 + 이체의 10%를 HIGH로 올려 보류 경로를 태움
# 구성을 번갈아 도는 이유: 한 구성을 몰아서 재면 그사이의 노트북 상태 변화(발열, 백그라운드 작업)가 구성 차이로 보인다.
# 측정마다 수천 행이 더 쌓이지만 100만 행에 비하면 작다.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPS=${1:-3}
CONCURRENCY=${2:-20}
DURATION=${3:-30}
ROWS=${4:-1000000}
PORT=8100
PYTHON=${PYTHON:-python}
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_USER=finops_user MYSQL_PASSWORD=finops_pass MYSQL_DB=finops

docker rm -f fds-loadtest-mysql >/dev/null 2>&1 || true
docker run -d --name fds-loadtest-mysql -p 3307:3306 \
  -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=finops \
  -e MYSQL_USER=finops_user -e MYSQL_PASSWORD=finops_pass mysql:8.0 >/dev/null
until docker exec fds-loadtest-mysql mysql -ufinops_user -pfinops_pass -e "select 1" finops >/dev/null 2>&1; do sleep 2; done

"$PYTHON" scripts/loadtest/seed_large.py --rows "$ROWS"
"$PYTHON" scripts/loadtest/seed_large.py --indexes off --explain
"$PYTHON" scripts/loadtest/seed_large.py --indexes on --explain

run_one() {   # $1 = 라벨, $2 = 서버 구성, $3 = 인덱스 on|off
  "$PYTHON" scripts/loadtest/seed_large.py --indexes "$3" >/dev/null
  "$PYTHON" scripts/loadtest/server.py --variant "$2" --port $PORT &
  SERVER=$!
  until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
    kill -0 $SERVER 2>/dev/null || { echo "서버가 시작되지 못했다 ($1)" >&2; exit 1; }
    sleep 1
  done
  "$PYTHON" scripts/loadtest/run.py --url "http://127.0.0.1:$PORT" --concurrency "$CONCURRENCY" --duration "$DURATION" --label "$1"
  kill $SERVER; wait $SERVER 2>/dev/null || true
}

for REP in $(seq 1 "$REPS"); do
  run_one "no-index#$REP" baseline off
  run_one "index#$REP" baseline on
  run_one "hold-10pct#$REP" hold-10pct on
done
ROWS_AFTER=$(docker exec fds-loadtest-mysql mysql -N -ufinops_user -pfinops_pass -e "select count(*) from transactions" finops 2>/dev/null)
echo "{\"transactions_rows_after\": $ROWS_AFTER}"
docker rm -f fds-loadtest-mysql >/dev/null 2>&1 || true
