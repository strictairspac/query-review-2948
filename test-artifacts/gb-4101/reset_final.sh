#!/bin/bash
# 重置最终验收环境：新库 + flush redis + 清理本程序索引 + 重启 backend/worker
set -u
DIR=/workspace/test-artifacts/gb-4101
PGBIN=/tmp/pgdir/postgresql-18.6.0-aarch64-unknown-linux-gnu/bin
pgrep -f 'run_final_backend.sh|run_final_worker.sh' | xargs -r kill 2>/dev/null
pgrep -f 'uvicorn src.main:app --host 127.0.0.1 --port 18001' | xargs -r kill 2>/dev/null
pgrep -f 'python3 -m src.worker' | xargs -r kill 2>/dev/null
sleep 1
$PGBIN/psql -h /tmp -p 55440 -U app -d postgres -q -c "DROP DATABASE IF EXISTS query_review_final" -c "CREATE DATABASE query_review_final"
~/.local/lib/python3.11/site-packages/redislite/bin/redis-cli -p 56380 flushall >/dev/null
cd /workspace/backend
DATABASE_URL='postgresql+psycopg2://app@/query_review_final?host=/tmp&port=55440' JWT_SECRET=test-secret python3 -m scripts.seed_demo >/dev/null
rm -rf $DIR/final-artifacts && mkdir -p $DIR/final-artifacts
setsid nohup $DIR/run_final_backend.sh > $DIR/final-backend.log 2>&1 < /dev/null &
setsid nohup $DIR/run_final_worker.sh > $DIR/final-worker.log 2>&1 < /dev/null &
for i in $(seq 1 20); do curl -s -m1 localhost:18001/health >/dev/null && break; sleep 1; done
curl -s localhost:18001/health; echo
