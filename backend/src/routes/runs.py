from fastapi import APIRouter,Depends,HTTPException
from src.config.database import get_db
from src.utils.auth import current_user,editor,admin
from src.schemas.run import RunInput,GenerationInput
from src.controllers.run_controller import list_runs,create_run,get_run
from src.services.generation_service import (create_generation,list_generations,get_generation_detail,
    get_generation,retry_generation,generation_diff,publish_generation)
router=APIRouter(prefix='/runs')
@router.get('')
def index(db=Depends(get_db),user=Depends(current_user)):return list_runs(db)
@router.post('',status_code=201)
def create(data:RunInput,db=Depends(get_db),user=Depends(editor)):return create_run(db,data,actor=user.email)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),user=Depends(current_user)):return get_run(db,id)
@router.get('/{id}/generations')
def generations(id:int,db=Depends(get_db),user=Depends(current_user)):return list_generations(db,id)
@router.post('/{id}/generations',status_code=201)
def new_generation(id:int,data:GenerationInput,db=Depends(get_db),user=Depends(editor)):
    gen=create_generation(db,id,data,actor=user.email,algorithm=data.algorithm,note=data.note)
    return {'generation_id':gen.id,'generation_no':gen.generation_no,'status':gen.status,
            'snapshot_fingerprint':gen.snapshot_fingerprint}
@router.get('/{id}/generations/{generation_id}')
def generation(id:int,generation_id:int,db=Depends(get_db),user=Depends(current_user)):
    return get_generation_detail(db,id,generation_id)
@router.post('/{id}/generations/{generation_id}/retry',status_code=202)
def retry(id:int,generation_id:int,db=Depends(get_db),user=Depends(editor)):
    gen=retry_generation(db,id,generation_id,actor=user.email)
    return {'generation_id':gen.id,'status':gen.status,'attempt':gen.attempt}
@router.post('/{id}/generations/{generation_id}/publish')
def publish(id:int,generation_id:int,db=Depends(get_db),user=Depends(admin)):
    gen=get_generation(db,id,generation_id)
    if gen.status not in ('ready','published'):
        raise HTTPException(409,'只有已通过逐馆核对的研究代可以发布')
    published=publish_generation(db,generation_id)
    return {'generation_id':generation_id,'published':published}
@router.get('/{id}/diff')
def diff(id:int,db=Depends(get_db),user=Depends(current_user),from_id:int|None=None,to_id:int|None=None):
    return generation_diff(db,id,from_id,to_id)
