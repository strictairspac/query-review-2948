#!/bin/bash
# 追加验收：真实建索引中途杀 worker；建索引期间账单可写；原查询只读。
set -u
B=http://127.0.0.1:18001
DIR=/workspace/test-artifacts/gb-4101
PGBIN=/tmp/pgdir/postgresql-18.6.0-aarch64-unknown-linux-gnu/bin
PSQL="$PGBIN/psql -h /tmp -p 55440 -U app -d query_review_final -Atc"
PSQLQ="$PGBIN/psql -h /tmp -p 55440 -U app -d query_review_final -q"
pass=0; fail=0
ok(){ echo "PASS  $1"; pass=$((pass+1)); }
no(){ echo "FAIL  $1"; fail=$((fail+1)); }
expect(){ [ "$1" = "$2" ] && ok "$3 ($2)" || no "$3 (want $1 got $2)"; }

ADMIN=$(curl -s -XPOST $B/auth/login -H 'content-type: application/json' -d '{"email":"admin@chenggang.test","password":"ColdChain!17"}' | jq -r .token)
GET(){ curl -s $B/index-trials/$1 -H "Authorization: Bearer $ADMIN" | jq -r "$2"; }

echo "== 10. 真实建索引中途断开"
curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["route","status"],"sample_id":1,"reason":"真实中断测试"}' >/dev/null
for i in $(seq 1 15); do s=$(curl -s $B/index-trials -H "Authorization: Bearer $ADMIN"|jq -r '.[]|select(.column_group=="route,status")|.status'); [ "$s" = pending ] || [ "$s" = building ] && break; sleep 1; done
T=$(curl -s $B/index-trials -H "Authorization: Bearer $ADMIN"|jq -r '.[]|select(.column_group=="route,status")|.id'); I=$(GET $T .index_name)
pgrep -f 'python3 -m src.worker' | xargs -r kill; sleep 1
$PSQL "DROP INDEX CONCURRENTLY IF EXISTS $I" >/dev/null 2>&1
$PSQL "update index_trials set status='pending', plan_before=null where id=$T" >/dev/null
# 长事务持有旧快照，使 CREATE INDEX CONCURRENTLY 停在等待快照阶段
( $PSQLQ -c "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM invoices;" -c "SELECT pg_sleep(20);" -c "COMMIT;" >/dev/null 2>&1 ) &
HOLD=$!; sleep 1
setsid nohup $DIR/run_final_worker.sh >> $DIR/final-worker.log 2>&1 < /dev/null &
for i in $(seq 1 15); do n=$($PSQL "select count(*) from pg_stat_activity where query like 'CREATE INDEX CONCURRENTLY%' and state<>'idle'"); [ "$n" -ge 1 ] && break; sleep 1; done
expect 1 "$n" "建索引进行中"
# 建索引期间账单写入
$PSQL "INSERT INTO invoices VALUES (99001,'建索引期间写入','东线',1.00,3.0,'pending')" >/dev/null 2>&1
w=$($PSQL "select count(*) from invoices where invoice_id=99001")
expect 1 "$w" "建索引期间账单仍可写"
# 建索引期间原有查询链路仍只读（query_reader 拒绝写）
q=$($PSQL "SET ROLE query_reader; INSERT INTO invoices VALUES (99002,'x','东线',1,1,'pending')" 2>&1 | grep -c 'permission denied')
expect 1 "$q" "建索引期间查询角色仍只读"
# 杀掉 worker（模拟建索引中途断开），并结束长事务
kill %2 2>/dev/null; pgrep -f 'python3 -m src.worker' | xargs -r kill -9; sleep 1
kill $HOLD 2>/dev/null; wait $HOLD 2>/dev/null
sleep 1
$PSQL "DELETE FROM invoices WHERE invoice_id=99001" >/dev/null
st=$($PSQL "select i.indisvalid from pg_class c join pg_index i on i.indexrelid=c.oid where c.relname='$I'")
[ -z "$st" ] && no "中断后应留有残留索引待恢复" || ok "中断后残留索引状态 indisvalid=$st"
$PSQL "update index_trials set status='building' where id=$T" >/dev/null
setsid nohup $DIR/run_final_worker.sh >> $DIR/final-worker.log 2>&1 < /dev/null &
for i in $(seq 1 20); do s=$(GET $T .status); [ "$s" = failed ] || [ "$s" = verified ] && break; sleep 1; done
[ "$s" = failed ] && ok "中断试验恢复为 failed" || no "中断试验恢复状态=$s"
expect 0 "$($PSQL "select count(*) from pg_indexes where indexname='$I'")" "中断残留索引已清理"
expect 1 "$($PSQL "select count(*) from pg_indexes where indexname='idx_trial_invoices_cd12d2580b8f'")" "已有索引不受影响"
ev=$(GET $T '.events|map(.reason)|join(" | ")')
echo "  轨迹: $ev"

echo "== 11. 并发试验同一列组只建一份（含 worker 并发）"
curl -s -XPOST $B/index-trials -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"columns":["temperature","status"],"sample_id":1,"reason":"并发列组"}' >/dev/null
for i in $(seq 1 20); do s=$(curl -s $B/index-trials -H "Authorization: Bearer $ADMIN"|jq -r '.[]|select(.column_group=="status,temperature")|.status'); [ "$s" = verified ] && break; sleep 1; done
expect verified "$s" "并发列组试验完成"
cnt=$($PSQL "select count(*) from pg_class c join pg_index i on i.indexrelid=c.oid where c.relname in (select index_name from index_trials where column_group='status,temperature')")
expect 1 "$cnt" "同一列组数据库只存在一份索引"

echo
echo "RESULT pass=$pass fail=$fail"
[ $fail -eq 0 ]
