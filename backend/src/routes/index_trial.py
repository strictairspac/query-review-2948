import json
from fastapi import APIRouter,Depends,Response
from ..config.database import get_db
from ..schemas.index_trial import IndexTrialInput,IndexTrialDecision,IndexTrialCleanup
from ..services.auth import current_user,admin
from ..services.index_trial import list_trials,get_trial,create_trial,decide_trial,request_cleanup

router=APIRouter(prefix='/index-trials')


@router.get('')
def listing(db=Depends(get_db),user=Depends(current_user)):return list_trials(db)


@router.get('/{trial_id}')
def detail(trial_id:int,db=Depends(get_db),user=Depends(current_user)):return get_trial(db,trial_id)


@router.get('/{trial_id}/export')
def export(trial_id:int,db=Depends(get_db),user=Depends(current_user)):
    payload=get_trial(db,trial_id)
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',
                    headers={'Content-Disposition':f'attachment; filename="index-trial-{trial_id}.json"'})


@router.post('',status_code=202)
def create(data:IndexTrialInput,db=Depends(get_db),user=Depends(admin)):
    trial,reused=create_trial(db,data,user)
    return {'trial':trial,'reused':reused}


@router.post('/{trial_id}/decision')
def decide(trial_id:int,data:IndexTrialDecision,db=Depends(get_db),user=Depends(admin)):
    return decide_trial(db,trial_id,data,user)


@router.post('/{trial_id}/cleanup',status_code=202)
def cleanup(trial_id:int,data:IndexTrialCleanup,db=Depends(get_db),user=Depends(admin)):
    return request_cleanup(db,trial_id,data,user)
