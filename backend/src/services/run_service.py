from sqlalchemy import select
from fastapi import HTTPException
from src.models import BenchmarkSet,EfficiencyRun,RunGeneration
from src.services.generation_service import create_generation,execute_generation,_generation_summary
from src.constants.enums import RunStatus,GenerationStatus,DEFAULT_ALGORITHM

def _derived_status(run,pub,active,failed):
    # A run is only "completed" when a generation actually published.
    if pub:return RunStatus.COMPLETED
    if active:return active[0].status
    if failed:return RunStatus.FAILED
    return run.status

def list_runs(db):
    runs=db.scalars(select(EfficiencyRun).order_by(EfficiencyRun.id.desc())).all()
    out=[]
    for run in runs:
        pub=db.get(RunGeneration,run.published_generation_id) if run.published_generation_id else None
        active=db.scalars(select(RunGeneration).where(RunGeneration.run_id==run.id,
            RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING,
                                      GenerationStatus.READY]))
            .order_by(RunGeneration.generation_no.desc())).all()
        failed=db.scalar(select(RunGeneration.id).where(RunGeneration.run_id==run.id,
            RunGeneration.status==GenerationStatus.FAILED))
        latest_failed=db.scalar(select(RunGeneration).where(RunGeneration.run_id==run.id,
            RunGeneration.status==GenerationStatus.FAILED)
            .order_by(RunGeneration.generation_no.desc()))
        out.append({'id':run.id,'name':run.name,'benchmark_id':run.benchmark_id,
                    'inputs':run.inputs,'outputs':run.outputs,
                    'sensitivity_count':run.sensitivity_count,
                    'status':_derived_status(run,pub,active,failed),
                    'error':(active[0].error if active and active[0].error else
                             latest_failed.error if latest_failed and not pub else run.error),
                    'created_at':run.created_at,
                    'algorithm':getattr(run,'algorithm',None) or DEFAULT_ALGORITHM,
                    'published_generation_id':run.published_generation_id,
                    'generation_count':run.generation_count,
                    'published_generation_no':pub.generation_no if pub else None,
                    'active_generation':_generation_summary(db,active[0]) if active else None,
                    'result':run.result})
    return out

def get_run(db,id):
    run=db.get(EfficiencyRun,id)
    if not run:raise HTTPException(404,'研究不存在')
    pub=db.get(RunGeneration,run.published_generation_id) if run.published_generation_id else None
    active=db.scalars(select(RunGeneration).where(RunGeneration.run_id==run.id,
        RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING,
                                  GenerationStatus.READY]))
        .order_by(RunGeneration.generation_no.desc())).all()
    failed=db.scalar(select(RunGeneration.id).where(RunGeneration.run_id==run.id,
        RunGeneration.status==GenerationStatus.FAILED))
    latest_failed=db.scalar(select(RunGeneration).where(RunGeneration.run_id==run.id,
        RunGeneration.status==GenerationStatus.FAILED)
        .order_by(RunGeneration.generation_no.desc()))
    return {'id':run.id,'name':run.name,'benchmark_id':run.benchmark_id,'inputs':run.inputs,
            'outputs':run.outputs,'sensitivity_count':run.sensitivity_count,
            'status':_derived_status(run,pub,active,failed),
            'error':(active[0].error if active and active[0].error else
                     latest_failed.error if latest_failed and not pub else run.error),
            'created_at':run.created_at,
            'algorithm':getattr(run,'algorithm',None) or DEFAULT_ALGORITHM,
            'published_generation_id':run.published_generation_id,
            'published_generation_no':pub.generation_no if pub else None,
            'generation_count':run.generation_count,
            'result':run.result}

def create_run(db,data,actor=''):
    if not db.get(BenchmarkSet,data.benchmark_id):raise HTTPException(404,'参照集不存在')
    if db.scalar(select(EfficiencyRun).where(EfficiencyRun.name==data.name)):raise HTTPException(409,'研究名称已存在')
    item=EfficiencyRun(**data.model_dump(),status=RunStatus.QUEUED,algorithm=DEFAULT_ALGORITHM);db.add(item);db.commit()
    # Research creation is generation 1; the run stays queued until that generation publishes.
    gen=create_generation(db,item.id,data,actor=actor)
    item=db.get(EfficiencyRun,item.id)
    return {'id':item.id,'name':item.name,'benchmark_id':item.benchmark_id,'inputs':item.inputs,
            'outputs':item.outputs,'sensitivity_count':item.sensitivity_count,
            'status':item.status,'error':item.error,'created_at':item.created_at,
            'algorithm':item.algorithm,'published_generation_id':None,'generation_count':1,
            'definitions':data.model_dump(),'generation_id':gen.id,'generation_no':gen.generation_no}

def execute_run(db,id):
    """Backward-compatible entry point: run the newest active generation of a research."""
    gen=db.scalars(select(RunGeneration).where(RunGeneration.run_id==id,
        RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING]))
        .order_by(RunGeneration.generation_no)).first()
    if not gen:return
    execute_generation(db,gen.id)
