"""普通组合索引试验：建前/建后真实计划、验证证据、保留或清理。

安全要点：
- 建索引使用 CREATE INDEX CONCURRENTLY，账单表在构建期间保持可写。
- 计划采集与验证沿用只读角色 + READ ONLY 事务，原有查询链路不变。
- 同一列组同时只允许一个活动试验；索引名由列组确定性生成，重试命中既有试验。
- 所有 DDL 以“列组咨询锁 + 索引 COMMENT 归属标记”保护：清理方只有在索引仍归属
  本次试验时才删除，因此清理与后发试验相撞不会误删后者，也不会动既有索引。
"""
import hashlib,json,secrets
from contextlib import contextmanager
from datetime import datetime
from sqlalchemy import select,update,text
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from ..config.database import Session,engine
from ..constants.enums import ACTIVE_TRIAL_STATUSES
from ..models.index_trial import IndexTrial,IndexTrialEvent
from ..models.sample import Sample
from ..services.batch import queue
from ..services.query_engine import ALLOWED_COLUMNS,execute_query

INDEX_TRIAL_QUEUE='index-trial:jobs'
INDEX_CLEANUP_QUEUE='index-trial:cleanup'
TRIAL_INDEX_PREFIX='idx_trial_invoices_'
LOCK_NAMESPACE=4101
BUSINESS_COLUMNS=tuple(sorted(ALLOWED_COLUMNS-{'invoice_id'}))
MAX_COLUMNS=4


def _canonical(columns):
    cols=[str(c).strip().lower() for c in columns]
    if not 2<=len(cols)<=MAX_COLUMNS:raise ValueError(f'请选择 2 到 {MAX_COLUMNS} 个业务列')
    if len(set(cols))!=len(cols):raise ValueError('业务列不能重复')
    unknown=[c for c in cols if c not in BUSINESS_COLUMNS]
    if unknown:raise ValueError('不可建索引的业务列：'+', '.join(unknown))
    cols=sorted(cols)
    return cols,','.join(cols)


def _index_name(group):
    return TRIAL_INDEX_PREFIX+hashlib.sha256(group.encode()).hexdigest()[:12]


def _owner_marker(trial):
    return f'index-trial:{trial.id}:{trial.owner_token}'


def _marker_trial_id(marker):
    try:return int(str(marker).split(':')[1])
    except Exception:return None


def _trial_status(trial_id):
    if not trial_id:return None
    with Session() as db:
        trial=db.get(IndexTrial,trial_id)
        return trial.status if trial else None


def _advisory_key(group):
    return int.from_bytes(hashlib.sha256(group.encode()).digest()[:4],'big')&0x7fffffff


@contextmanager
def _autocommit_conn():
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        yield conn


def _lock(conn,group):
    conn.execute(text('SELECT pg_advisory_lock(:n,:k)'),{'n':LOCK_NAMESPACE,'k':_advisory_key(group)})


def _unlock(conn,group):
    conn.execute(text('SELECT pg_advisory_unlock(:n,:k)'),{'n':LOCK_NAMESPACE,'k':_advisory_key(group)})


def _inspect_index(conn,trial):
    row=conn.execute(text("""
        SELECT c.relname,i.indisvalid,i.indisready,obj_description(c.oid,'pg_class') AS marker,
               (SELECT array_agg(a.attname ORDER BY a.attname)
                  FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum,ord)
                  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum) AS columns
        FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relname=:name AND n.nspname=current_schema()
    """),{'name':trial.index_name}).mappings().first()
    return dict(row) if row else None


def _is_owned(trial,info):
    """索引归属判定。

    1. COMMENT 标记完全一致 → 本试验所有。
    2. COMMENT 指向其他试验 → 他人所有，绝不动。
    3. 无标记但索引名是本列组的确定性名称，且列与本次试验一致 → 视为本次试验
       中断在 COMMENT 之前的残留（CONCURRENTLY 失败会留下无标记的无效索引）。
    """
    if not info:return False
    marker=info.get('marker')
    if marker==_owner_marker(trial):return True
    if marker:return False
    return (info.get('relname')==_index_name(trial.column_group)
            and sorted(info.get('columns') or [])==sorted(trial.columns))


def _drop_owned_index(trial,reason):
    """仅当索引仍归属本试验时删除；返回 (是否删除, 说明)。"""
    with _autocommit_conn() as conn:
        _lock(conn,trial.column_group)
        try:
            info=_inspect_index(conn,trial)
            if not info:return False,'索引不存在，无需删除'
            if not _is_owned(trial,info):
                return False,'索引已归属其他试验，跳过删除'
            conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{trial.index_name}"'))
            return True,f'已删除索引 {trial.index_name}（{reason}）'
        finally:_unlock(conn,trial.column_group)


def _event(db,trial,from_status,to_status,reason,actor,detail=None):
    db.add(IndexTrialEvent(trial_id=trial.id,from_status=from_status,to_status=to_status,reason=reason,actor=actor,detail=detail))


def _serialize_event(e):
    return {'id':e.id,'from_status':e.from_status,'to_status':e.to_status,'reason':e.reason,'actor':e.actor,'detail':e.detail,'created_at':str(e.created_at)}


def _serialize(trial,with_plans=True):
    data={'id':trial.id,'column_group':trial.column_group,'columns':trial.columns,'index_name':trial.index_name,
          'sample_id':trial.sample_id,'status':trial.status,'requested_by':trial.requested_by,
          'requested_reason':trial.requested_reason,'decision':trial.decision,'decision_reason':trial.decision_reason,
          'decision_by':trial.decision_by,'decision_evidence_sha256':trial.decision_evidence_sha256,
          'evidence_sha256':trial.evidence_sha256,'decided_at':str(trial.decided_at) if trial.decided_at else None,
          'last_error':trial.last_error,'created_at':str(trial.created_at),'updated_at':str(trial.updated_at),
          'index_ownership':{'trial_id':trial.id,'index_name':trial.index_name,'column_group':trial.column_group,
                             'attribution':f'index-trial:#{trial.id}',
                             'marker_sha256':hashlib.sha256(_owner_marker(trial).encode()).hexdigest()}}
    if with_plans:
        data.update(plan_before=trial.plan_before,plan_after=trial.plan_after,comparison=trial.comparison)
    return data


def list_trials(db):
    trials=list(db.scalars(select(IndexTrial).order_by(IndexTrial.id.desc())))
    events=list(db.scalars(select(IndexTrialEvent).order_by(IndexTrialEvent.id)))
    grouped={}
    for e in events:grouped.setdefault(e.trial_id,[]).append(_serialize_event(e))
    result=[]
    for t in trials:
        item=_serialize(t)
        item['events']=grouped.get(t.id,[])
        result.append(item)
    return result


def get_trial(db,trial_id):
    trial=db.get(IndexTrial,trial_id)
    if not trial:raise HTTPException(404,'索引试验不存在')
    data=_serialize(trial)
    data['events']=[_serialize_event(e) for e in db.scalars(select(IndexTrialEvent).where(IndexTrialEvent.trial_id==trial_id).order_by(IndexTrialEvent.id))]
    return data


def create_trial(db,data,user):
    columns,group=_canonical(data.columns)
    if not db.get(Sample,data.sample_id):raise HTTPException(404,'基准样本不存在')
    existing=db.scalar(select(IndexTrial).where(IndexTrial.column_group==group,IndexTrial.status.in_(ACTIVE_TRIAL_STATUSES)))
    if existing:return _serialize(existing),True
    trial=IndexTrial(column_group=group,columns=columns,index_name=_index_name(group),sample_id=data.sample_id,
                     status='pending',owner_token=secrets.token_hex(16),requested_by=user.email,
                     requested_reason=data.reason or '')
    db.add(trial)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing=db.scalar(select(IndexTrial).where(IndexTrial.column_group==group,IndexTrial.status.in_(ACTIVE_TRIAL_STATUSES)))
        if existing:return _serialize(existing),True
        raise HTTPException(409,'同一列组已有进行中的索引试验')
    _event(db,trial,None,'pending',data.reason or '管理员发起索引试验',user.email,
           {'columns':columns,'index_name':trial.index_name})
    db.commit()
    queue.rpush(INDEX_TRIAL_QUEUE,trial.id)
    return _serialize(trial),False


def decide_trial(db,trial_id,data,user):
    trial=db.scalar(select(IndexTrial).where(IndexTrial.id==trial_id).with_for_update())
    if not trial:raise HTTPException(404,'索引试验不存在')
    if trial.status!='verified':raise HTTPException(409,'只有验证完成的试验才能保留或清理')
    if not trial.evidence_sha256:raise HTTPException(409,'试验缺少验证证据，不能发布决定')
    if data.evidence_sha256 and data.evidence_sha256!=trial.evidence_sha256:
        raise HTTPException(409,'决定所附证据摘要与本次验证不一致，请重新查看试验证据')
    trial.decision=data.decision
    trial.decision_reason=data.reason
    trial.decision_by=user.email
    trial.decision_evidence_sha256=trial.evidence_sha256
    trial.decided_at=datetime.utcnow()
    if data.decision=='retain':
        trial.status='retained'
        _event(db,trial,'verified','retained','保留索引：'+data.reason,user.email,
               {'evidence_sha256':trial.evidence_sha256,'index_name':trial.index_name})
    else:
        trial.status='cleaning'
        _event(db,trial,'verified','cleaning','清理索引：'+data.reason,user.email,
               {'evidence_sha256':trial.evidence_sha256,'index_name':trial.index_name})
    db.commit()
    if data.decision=='cleanup':queue.rpush(INDEX_CLEANUP_QUEUE,trial.id)
    return _serialize(trial)


def request_cleanup(db,trial_id,data,user):
    trial=db.scalar(select(IndexTrial).where(IndexTrial.id==trial_id).with_for_update())
    if not trial:raise HTTPException(404,'索引试验不存在')
    if trial.status not in ('failed','verified','retained'):raise HTTPException(409,'仅失败、已验证或已保留试验可请求清理')
    previous=trial.status
    trial.status='cleaning'
    _event(db,trial,previous,'cleaning','人工清理：'+data.reason,user.email,{'index_name':trial.index_name})
    db.commit()
    queue.rpush(INDEX_CLEANUP_QUEUE,trial.id)
    return _serialize(trial)


def _measure(db,trial):
    sample=db.get(Sample,trial.sample_id)
    if not sample:raise RuntimeError('基准样本不存在')
    result=execute_query(sample.original_sql)
    rows=result.pop('rows',[])
    payload=json.dumps(rows,ensure_ascii=False,separators=(',',':'))
    result['result_checksum']=hashlib.sha256(payload.encode()).hexdigest()
    return result


def _plan_metrics(plan):
    root=(plan or [{}])[0] if isinstance(plan,list) and plan else {}
    node=root.get('Plan') or {}
    nodes=[]
    def walk(n):
        nodes.append(n)
        for child in n.get('Plans') or []:walk(child)
    walk(node)
    scans=[n.get('Node Type','') for n in nodes if n.get('Node Type','').endswith('Scan')]
    return {'planning_time_ms':root.get('Planning Time'),'execution_time_ms':root.get('Execution Time'),
            'total_cost':node.get('Total Cost'),'plan_rows':node.get('Plan Rows'),
            'actual_rows':node.get('Actual Rows'),'node_types':[n.get('Node Type') for n in nodes],
            'scan_nodes':[{'node_type':n.get('Node Type'),'relation':n.get('Relation Name'),
                           'index':n.get('Index Name'),'actual_rows':n.get('Actual Rows')} for n in nodes
                          if n.get('Node Type','').endswith('Scan')],
            'seq_scan_count':sum(1 for s in scans if s=='Seq Scan'),
            'index_scan_count':sum(1 for s in scans if 'Index' in s),
            'shared_hit_blocks':node.get('Shared Hit Blocks'),'shared_read_blocks':node.get('Shared Read Blocks')}


def _delta(before,after):
    if before in (None,0) or after is None:return None
    return round((after-before)/before*100,2)


def compare_plans(before,after,trial_index=None):
    b=_plan_metrics(before.get('plan'));a=_plan_metrics(after.get('plan'))
    used=sorted({s['index'] for s in a['scan_nodes'] if s.get('index')})
    used_trial=[name for name in used if name==trial_index]
    result_unchanged=before.get('result_checksum')==after.get('result_checksum') and before.get('row_count')==after.get('row_count')
    cost_ratio=_delta(b['total_cost'],a['total_cost'])
    exec_delta=round((a['execution_time_ms'] or 0)-(b['execution_time_ms'] or 0),3) if b['execution_time_ms'] is not None and a['execution_time_ms'] is not None else None
    verdict_parts=[]
    if used_trial:verdict_parts.append('建后计划使用本次试验索引：'+', '.join(used_trial))
    else:verdict_parts.append('建后优化器未选择本次试验索引（数据量小或选择度低时属正常）')
    if cost_ratio is not None:verdict_parts.append(f'估算总代价变化 {cost_ratio:+.2f}%')
    verdict_parts.append('结果行与摘要一致' if result_unchanged else '结果行或摘要发生变化，需人工核查')
    return {'before':b,'after':a,'cost_delta_ratio':cost_ratio,
            'execution_delta_ms':exec_delta,
            'execution_delta_ratio':_delta(b['execution_time_ms'],a['execution_time_ms']),
            'index_used_after':bool(used_trial),'indexes_used_after':used,'trial_index_used':used_trial,
            'results_unchanged':result_unchanged,'verdict':'；'.join(verdict_parts),
            'note':'计划与耗时为实际执行采集；耗时受缓存与机器负载影响，单次结果不能断言优化收益。'}


def _evidence_checksum(trial,before,after,comparison):
    payload={'trial_id':trial.id,'index_name':trial.index_name,'columns':trial.columns,'column_group':trial.column_group,
             'sample_id':trial.sample_id,'owner_token':trial.owner_token,'requested_reason':trial.requested_reason,
             'plan_before':before,'plan_after':after,'comparison':comparison}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _fail(trial_id,message,actor='worker'):
    with Session() as db:
        trial=db.get(IndexTrial,trial_id)
        if not trial:return
        previous=trial.status
        trial.status='failed';trial.last_error=message[:2000]
        _event(db,trial,previous,'failed',message[:500],actor,{'last_error':message[:2000]})
        db.commit()
        try:
            dropped,note=_drop_owned_index(trial,'试验失败清理')
            if dropped:_event(db,trial,'failed','failed','失败后清理残留索引：'+note,actor,{'index_name':trial.index_name});db.commit()
        except Exception as exc:
            db.add(IndexTrialEvent(trial_id=trial.id,from_status='failed',to_status='failed',
                                   reason='失败后清理索引未完成：'+str(exc)[:300],actor=actor));db.commit()


def _build_index(trial_id):
    with Session() as db:schema_trial=db.get(IndexTrial,trial_id)
    with _autocommit_conn() as conn:
        _lock(conn,schema_trial.column_group)
        try:
            info=_inspect_index(conn,schema_trial)
            marker=_owner_marker(schema_trial)
            if info and not _is_owned(schema_trial,info):
                owner_id=_marker_trial_id(info['marker'])
                owner_status=_trial_status(owner_id)
                if owner_status not in ('failed','cleaned','cleaning'):
                    raise RuntimeError('同名索引已归属进行中的其他试验，拒绝覆盖')
                # 前一试验已终止或已进入清理队列：持锁接替，先清掉再重建。
                # 旧试验稍后的清理会因 COMMENT 归属已变而跳过，不会误删新索引。
                conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{schema_trial.index_name}"'))
                info=None
            if info and not info['indisvalid']:
                # 上次中断留下的无效索引，先清掉再重建（仍持有列组锁）。
                conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{schema_trial.index_name}"'))
                info=None
            if not info:
                cols=', '.join(f'"{c}"' for c in schema_trial.columns)
                conn.execute(text(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{schema_trial.index_name}" ON invoices ({cols})'))
            conn.execute(text("COMMENT ON INDEX \"%s\" IS :m" % schema_trial.index_name),{'m':marker})
            info=_inspect_index(conn,schema_trial)
            if not info or not info['indisvalid']:
                raise RuntimeError('索引构建后仍无效，请重试')
        finally:_unlock(conn,schema_trial.column_group)


def _verify(trial_id):
    with Session() as db:
        trial=db.get(IndexTrial,trial_id)
        if not trial or trial.status not in ('building','verifying'):return
        before=trial.plan_before
        if not before:raise RuntimeError('缺少建前计划，无法完成验证')
        if trial.status=='building':
            trial.status='verifying';_event(db,trial,'building','verifying','索引已建好，开始验证建后计划','worker');db.commit()
        after=_measure(db,trial)
        comparison=compare_plans(before,after,trial.index_name)
        checksum=_evidence_checksum(trial,before,after,comparison)
        trial.plan_after=after;trial.comparison=comparison;trial.evidence_sha256=checksum
        trial.status='verified';trial.last_error=None
        _event(db,trial,'verifying','verified','验证完成，等待管理员复核保留或清理','worker',
               {'evidence_sha256':checksum,'verdict':comparison['verdict']})
        db.commit()
    _write_artifact(trial_id)


def run_trial(trial_id):
    with Session() as db:
        trial=db.get(IndexTrial,trial_id)
        if not trial or trial.status!='pending':return
        changed=db.execute(update(IndexTrial).where(IndexTrial.id==trial_id,IndexTrial.status=='pending')
                           .values(status='building',updated_at=datetime.utcnow())).rowcount
        db.commit()
        if not changed:return
        trial=db.get(IndexTrial,trial_id)
        _event(db,trial,'pending','building','开始采集建前计划并创建索引','worker',{'index_name':trial.index_name})
        db.commit()
        try:
            before=_measure(db,trial)
            trial.plan_before=before;db.commit()
        except Exception as exc:
            _fail(trial_id,'采集建前计划失败：'+str(exc)[:400]);return
    try:
        _build_index(trial_id)
    except Exception as exc:
        _fail(trial_id,'创建索引失败：'+str(exc)[:400]);return
    try:
        _verify(trial_id)
    except Exception as exc:
        _fail(trial_id,'验证建后计划失败：'+str(exc)[:400])


def run_cleanup(trial_id):
    with Session() as db:
        trial=db.get(IndexTrial,trial_id)
        if not trial or trial.status!='cleaning':return
    try:
        dropped,note=_drop_owned_index(trial,'清理请求')
        with Session() as db:
            trial=db.get(IndexTrial,trial_id)
            # 若期间该列组已被新试验接替，索引归属已改变，清理方只记录、不覆盖新试验。
            previous=trial.status
            if trial.status!='cleaning':return
            trial.status='cleaned';trial.updated_at=datetime.utcnow()
            _event(db,trial,previous,'cleaned',note,'worker',{'dropped':dropped,'index_name':trial.index_name})
            db.commit()
    except Exception as exc:
        with Session() as db:
            trial=db.get(IndexTrial,trial_id)
            trial.status='failed';trial.last_error=('清理索引失败：'+str(exc)[:400])
            _event(db,trial,'cleaning','failed',trial.last_error,'worker');db.commit()


def _write_artifact(trial_id):
    try:
        import os
        from pathlib import Path
        with Session() as db:payload=get_trial(db,trial_id)
        artifact_dir=Path(os.getenv('ARTIFACT_DIR','/artifacts'))
        artifact_dir.mkdir(parents=True,exist_ok=True)
        (artifact_dir/f'index-trial-{trial_id}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    except Exception:pass


def recover_trials():
    """worker 启动/周期恢复：中断试验的残留无效索引清理，可继续的验证继续。"""
    with Session() as db:
        candidates=list(db.scalars(select(IndexTrial.id).where(IndexTrial.status.in_(['building','verifying','failed','cleaning']))))
    for trial_id in candidates:
        try:_recover_one(trial_id)
        except Exception:pass


def _recover_one(trial_id):
    with Session() as db:trial=db.get(IndexTrial,trial_id)
    if not trial:return
    if trial.status=='cleaning':
        run_cleanup(trial_id);return
    if trial.status in ('building','verifying'):
        with _autocommit_conn() as conn:
            _lock(conn,trial.column_group)
            try:info=_inspect_index(conn,trial)
            finally:_unlock(conn,trial.column_group)
        if info and info['indisvalid'] and info['marker']==_owner_marker(trial):
            with Session() as db:
                trial=db.get(IndexTrial,trial_id);previous=trial.status;trial.status='verifying'
                _event(db,trial,previous,'verifying','worker 恢复：索引有效，继续验证','recovery');db.commit()
            try:_verify(trial_id)
            except Exception as exc:_fail(trial_id,'恢复后验证失败：'+str(exc)[:400],'recovery')
            return
        dropped,note=_drop_owned_index(trial,'中断残留清理')
        with Session() as db:
            trial=db.get(IndexTrial,trial_id);previous=trial.status
            trial.status='failed'
            trial.last_error=('worker 中断，已清理本次残留索引；可重新发起试验' if dropped or not info
                              else 'worker 中断，残留索引归属已变化，未删除；'+note)
            _event(db,trial,previous,'failed',trial.last_error,'recovery',
                   {'dropped':dropped,'cleanup':note,'index_name':trial.index_name})
            db.commit()
        return
    if trial.status=='failed':
        # 只为仍有残留索引的失败试验做复查，避免每次启动对历史记录重复加锁。
        with _autocommit_conn() as conn:info=_inspect_index(conn,trial)
        if not info:return
        dropped,note=_drop_owned_index(trial,'失败残留复查')
        if dropped:
            with Session() as db:
                db.add(IndexTrialEvent(trial_id=trial.id,from_status='failed',to_status='failed',
                                       reason='恢复时清理失败残留索引：'+note,actor='recovery'));db.commit()
