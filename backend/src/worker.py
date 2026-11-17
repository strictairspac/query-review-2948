import os,time,json,traceback
from pathlib import Path
from sqlalchemy import select
from .config.database import Session
from .models.batch import Batch
from .models.sample import Sample
from .models.evidence import Evidence
from .services.batch import queue
from .services.query_engine import compare
from .services.index_trial import INDEX_CLEANUP_QUEUE,INDEX_TRIAL_QUEUE,recover_trials,run_cleanup,run_trial
ARTIFACT_DIR=Path(os.getenv('ARTIFACT_DIR','/artifacts'))
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
heartbeat=ARTIFACT_DIR/'worker.heartbeat'


def run_one(batch_id):
    with Session() as db:
        batch=db.get(Batch,batch_id)
        if not batch or db.scalar(select(Evidence).where(Evidence.batch_id==batch_id)):return
        sample=db.get(Sample,batch.sample_id);batch.status='running';db.commit()
        try:
            result=compare(sample.original_sql,batch.candidate_sql)
            batch.status='passed' if result['equivalent'] else 'different'
        except Exception as exc:
            result={'error':str(exc)[:500]};batch.status='failed'
        db.add(Evidence(batch_id=batch_id,result=result));db.commit()
        ARTIFACT_DIR/f'review-{batch_id}.json'.write_text(json.dumps(result,ensure_ascii=False,indent=2))


def drain(queue_name,handler):
    item=queue.lpop(queue_name)
    if not item:return False
    handler(int(item))
    return True


recovered=False
while True:
    try:
        heartbeat.write_text(str(time.time()))
        if not recovered:
            try:recover_trials();recovered=True
            except Exception:traceback.print_exc()
        if drain(INDEX_TRIAL_QUEUE,run_trial):continue
        if drain(INDEX_CLEANUP_QUEUE,run_cleanup):continue
        item=queue.blpop(['query-review:jobs',INDEX_TRIAL_QUEUE,INDEX_CLEANUP_QUEUE],timeout=2)
        if item:
            name,ident=item
            if name==b'query-review:jobs':run_one(int(ident))
            elif name==INDEX_TRIAL_QUEUE.encode():run_trial(int(ident))
            else:run_cleanup(int(ident))
        else:
            with Session() as db:pending=list(db.scalars(select(Batch.id).where(Batch.status.in_(['queued','running']))))
            for ident in pending:run_one(ident)
    except Exception:traceback.print_exc();time.sleep(2)
