#!/bin/bash
# 索引试验验收脚本：覆盖页面可发起的 API、拒绝、重复、中断恢复、清理撞新试验。
set -u
B=http://127.0.0.1:18001
DIR=/workspace/test-artifacts/gb-4101
PSQL="/tmp/pgdir/postgresql-18.6.0-aarch64-unknown-linux-gnu/bin/psql -h /tmp -p 55440 -U app -d query_review_final -Atc"
pass=0; fail=0
ok(){ echo "PASS  $1"; pass=$((pass+1)); }
no(){ echo "FAIL  $1"; fail=$((fail+1)); }
expect(){ local want=$1 got=$2 name=$3; [ "$want" = "$got" ] && ok "$name ($got)" || no "$name (want $want got $got)"; }
jqget(){ jq -r "$1"; }

login(){ curl -s -XPOST $B/auth/login -H 'content-type: application/json' -d "{\"email\":\"$1\",\"password\":\"ColdChain!17\"}" | jqget .token; }
ADMIN=$(login admin@chenggang.test); OP=$(login operator@chenggang.test); VIEW=$(login viewer@chenggang.test)
echo "== 1. 权限：非管理员不能发起/复核"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials -H "Authorization: Bearer $OP" -H 'content-type: application/json' -d '{"columns":["customer","temperature"],"sample_id":1,"reason":"op"}')
expect 403 "$code" "operator 发起被拒"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials -H "Authorization: Bearer $VIEW" -H 'content-type: application/json' -d '{"columns":["customer","temperature"],"sample_id":1,"reason":"viewer"}')
expect 403 "$code" "viewer 发起被拒"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials -H 'content-type: application/json' -d '{"columns":["customer","temperature"],"sample_id":1}')
[ "$code" = 401 ] || [ "$code" = 403 ] && ok "匿名发起被拒 ($code)" || no "匿名发起被拒 ($code)"
code=$(curl -s -o /dev/null -w '%{http_code}' $B/index-trials -H "Authorization: Bearer $VIEW")
expect 200 "$code" "viewer 可查看试验"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["temperature"],"sample_id":1,"reason":"only one"}')
expect 422 "$code" "只选 1 列被拒"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["customer","secret"],"sample_id":1,"reason":"bad col"}')
expect 422 "$code" "非白名单列被拒"

echo "== 2. 发起 + 重复提交复用同一试验"
r1=$(curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["temperature","customer"],"sample_id":1,"reason":"结算汇总按客户与温度过滤"}')
T1=$(echo "$r1"|jqget .trial.id); expect "$T1" "$(echo "$r1"|jqget .trial.id)" "首次发起返回试验 #$T1"
r2=$(curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["customer","temperature"],"sample_id":1,"reason":"重复提交"}')
expect "$T1" "$(echo "$r2"|jqget .trial.id)" "重复提交复用同一试验"; expect true "$(echo "$r2"|jqget .reused)" "重复提交 reused=true"
# 8 路并发同列组
for i in $(seq 1 8); do curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["route","amount"],"sample_id":2,"reason":"并发'$i'"}' > $DIR/conc$i.json & done; wait
T2=$(jq -sr '[.[].trial.id]|unique|.[0]' $DIR/conc*.json); n=$(jq -sr '[.[].trial.id]|unique|length' $DIR/conc*.json)
expect 1 "$n" "8 路并发同列组只产生 1 个试验"; rm -f $DIR/conc*.json

echo "== 3. worker 完成验证，建前/建后计划与比较结果"
for i in $(seq 1 30); do s=$(curl -s $B/index-trials/$T1 -H "Authorization: Bearer $ADMIN"|jqget .status); [ "$s" = verified ] && break; sleep 1; done
expect verified "$s" "试验 #$T1 验证完成"
d=$(curl -s $B/index-trials/$T1 -H "Authorization: Bearer $ADMIN")
expect true "$(echo "$d"|jq '.plan_before!=null and .plan_after!=null')" "保存建前与建后实际计划"
expect true "$(echo "$d"|jq '.comparison.results_unchanged')" "建前建后结果摘要一致"
expect true "$(echo "$d"|jq '.comparison.index_used_after')" "建后计划使用新索引"
SHA=$(echo "$d"|jqget .evidence_sha256)
idx=$($PSQL "select count(*) from pg_indexes where indexname='$(echo "$d"|jqget .index_name)'")
expect 1 "$idx" "列组索引实际存在"

echo "== 4. 复核决定与证据绑定"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials/$T1/decision -H "Authorization: Bearer $OP" -H 'content-type: application/json' -d '{"decision":"retain","reason":"op"}')
expect 403 "$code" "operator 发布决定被拒"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/index-trials/$T1/decision -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"decision":"retain","reason":"证据不一致","evidence_sha256":"bad"}')
expect 409 "$code" "证据摘要不符被拒"
r=$(curl -s -XPOST $B/index-trials/$T1/decision -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d "{\"decision\":\"retain\",\"reason\":\"结算汇总确认走索引\",\"evidence_sha256\":\"$SHA\"}")
expect retained "$(echo "$r"|jqget .status)" "保留决定落库"
expect "$SHA" "$(echo "$r"|jqget .decision_evidence_sha256)" "决定绑定同一证据摘要"
expect 1 "$($PSQL "select count(*) from pg_indexes where indexname='$(echo "$d"|jqget .index_name)'")" "保留后索引仍在"

echo "== 5. 证据下载包含索引归属"
curl -s $B/index-trials/$T1/export -H "Authorization: Bearer $VIEW" -o $DIR/export-$T1.json
owner=$(jq -r '.index_ownership.attribution' $DIR/export-$T1.json)
expect "index-trial:#$T1" "$owner" "导出证据含索引归属"
expect "$SHA" "$(jq -r .evidence_sha256 $DIR/export-$T1.json)" "导出证据含验证摘要"
expect true "$(jq '(.events|length)>0' $DIR/export-$T1.json)" "导出证据含可查轨迹"

echo "== 6. 清理流程（验证后决定清理）"
for i in $(seq 1 20); do s=$(curl -s $B/index-trials/$T2 -H "Authorization: Bearer $ADMIN"|jqget .status); [ "$s" = verified ] && break; sleep 1; done
SHA2=$(curl -s $B/index-trials/$T2 -H "Authorization: Bearer $ADMIN"|jqget .evidence_sha256)
I2=$(curl -s $B/index-trials/$T2 -H "Authorization: Bearer $ADMIN"|jqget .index_name)
curl -s -XPOST $B/index-trials/$T2/decision -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d "{\"decision\":\"cleanup\",\"reason\":\"路线样本无需该索引\",\"evidence_sha256\":\"$SHA2\"}" >/dev/null
for i in $(seq 1 20); do s=$(curl -s $B/index-trials/$T2 -H "Authorization: Bearer $ADMIN"|jqget .status); [ "$s" = cleaned ] && break; sleep 1; done
expect cleaned "$s" "清理完成"
expect 0 "$($PSQL "select count(*) from pg_indexes where indexname='$I2'")" "清理后索引已删除"

echo "== 7. 中断/失败恢复：残留无效索引被清掉，已有索引不受影响"
BEFORE=$($PSQL "select string_agg(indexname,',' order by indexname) from pg_indexes where indexname like 'idx_trial%'")
# 造一个 verified 试验，停 worker 后把状态改为 building，并留下同名的无标记无效索引
curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["amount","temperature"],"sample_id":1,"reason":"中断测试"}' >/dev/null
for i in $(seq 1 20); do s=$(curl -s $B/index-trials -H "Authorization: Bearer $ADMIN"|jq -r '.[]|select(.column_group=="amount,temperature")|.status'); [ "$s" = verified ] && break; sleep 1; done
T3=$(curl -s $B/index-trials -H "Authorization: Bearer $ADMIN"|jq -r '.[]|select(.column_group=="amount,temperature")|.id')
I3=$(curl -s $B/index-trials/$T3 -H "Authorization: Bearer $ADMIN"|jqget .index_name)
worker_pid=$(pgrep -f 'python3 -m src.worker' | head -1); [ -n "$worker_pid" ] && kill $worker_pid; sleep 1
$PSQL "DROP INDEX CONCURRENTLY IF EXISTS $I3" >/dev/null 2>&1
PGOPTIONS='-c statement_timeout=1ms' /tmp/pgdir/postgresql-18.6.0-aarch64-unknown-linux-gnu/bin/psql -h /tmp -p 55440 -U app -d query_review_final -c "CREATE INDEX CONCURRENTLY $I3 ON invoices (amount, temperature)" >/dev/null 2>&1
$PSQL "update index_trials set status='building' where id=$T3" >/dev/null
iv=$($PSQL "select i.indisvalid from pg_class c join pg_index i on i.indexrelid=c.oid where c.relname='$I3'")
expect f "$iv" "已制造无效残留索引"
setsid nohup /workspace/test-artifacts/gb-4101/run_final_worker.sh >> $DIR/final-worker.log 2>&1 < /dev/null & sleep 5
for i in $(seq 1 20); do s=$(curl -s $B/index-trials/$T3 -H "Authorization: Bearer $ADMIN"|jqget .status); [ "$s" = failed ] && break; sleep 1; done
expect failed "$s" "中断试验标记 failed"
expect 0 "$($PSQL "select count(*) from pg_indexes where indexname='$I3'")" "中断残留索引被清理"
AFTER=$($PSQL "select string_agg(indexname,',' order by indexname) from pg_indexes where indexname like 'idx_trial%'")
expect "$BEFORE" "$AFTER" "已有索引不受影响"

echo "== 8. 重试不重复建索引（同列组失败后再发起）"
r=$(curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["temperature","amount"],"sample_id":1,"reason":"失败后重试"}')
expect false "$(echo "$r"|jqget .reused)" "失败后允许重新发起"
T4=$(echo "$r"|jqget .trial.id)
for i in $(seq 1 25); do s=$(curl -s $B/index-trials/$T4 -H "Authorization: Bearer $ADMIN"|jqget .status); [ "$s" = verified ] && break; sleep 1; done
expect verified "$s" "重试试验验证完成"
expect 1 "$($PSQL "select count(*) from pg_indexes where indexname='$I3'")" "重试只建一份索引"

echo "== 9. 清理与新试验相撞不误删后者"
T5=$($PSQL "select id from index_trials where status='retained' limit 1")
I5=$($PSQL "select index_name from index_trials where id=$T5")
curl -s -XPOST $B/index-trials/$T5/cleanup -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"reason":"相撞前置：清理旧试验"}' >/dev/null
worker_pid=$(pgrep -f 'python3 -m src.worker' | head -1); [ -n "$worker_pid" ] && kill $worker_pid; sleep 1
G=$($PSQL "select column_group from index_trials where id=$T5")
curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d "{\"columns\":[\"$(echo $G|cut -d, -f1)\",\"$(echo $G|cut -d, -f2)\"],\"sample_id\":1,\"reason\":\"相撞新试验\"}" >/dev/null
cd /workspace/backend && DATABASE_URL='postgresql+psycopg2://app@/query_review_final?host=/tmp&port=55440' REDIS_URL='redis://127.0.0.1:56380/0' JWT_SECRET='test-secret' ARTIFACT_DIR=$DIR/final-artifacts python3 - <<'PY'
from sqlalchemy import select
from src.config.database import Session
from src.models.index_trial import IndexTrial
from src.services.index_trial import run_trial,run_cleanup
with Session() as db:
    rows=list(db.scalars(select(IndexTrial).where(IndexTrial.status.in_(['pending','cleaning'])).order_by(IndexTrial.id)))
    pending=[r.id for r in rows if r.status=='pending']; cleaning=[r.id for r in rows if r.status=='cleaning']
for t in pending:run_trial(t)
for t in cleaning:run_cleanup(t)
PY
expect 1 "$($PSQL "select count(*) from pg_indexes where indexname='$I5'")" "新试验索引未被旧清理误删"
expect "$($PSQL "select id from index_trials where index_name='$I5' and status='verified'")" "$($PSQL "select id from index_trials where index_name='$I5' and status='verified'")" "新试验仍处 verified"

echo
echo "RESULT pass=$pass fail=$fail"
[ $fail -eq 0 ]
