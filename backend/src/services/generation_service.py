"""Generation-based recompute for one research (EfficiencyRun).

Lifecycle per generation:
    queued -> running -> ready -> published
                      \\-> failed  (failure_scope lists the exact parts)
    any in-flight generation may become superseded when a newer one is created
    or when a newer one publishes first.

Invariants:
  * `EfficiencyRun.result` is only replaced inside `publish_generation`, which locks
    the run row, re-checks generation order and the published generation id.
  * A retry reuses completed parts (only missing/failed venues are recomputed).
  * Part completion is guarded by an attempt token, so a late finish from an old
    worker attempt cannot overwrite a newer part result.
  * Verification runs per venue (θ, solver feasibility state, sample fingerprints);
    a single failing part blocks publication of the whole generation.
"""
from datetime import datetime
from sqlalchemy import select,update
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models import BenchmarkSet,EfficiencyRun,RunGeneration,GenerationPart
from src.services.observation_service import list_observations
from src.constants.enums import (RunStatus,GenerationStatus,GenerationPartStatus,
                                 DEFAULT_ALGORITHM,SUPPORTED_ALGORITHMS,ACTIVE_GENERATION_STATUSES)
from src.services import dea_pipeline as pipeline
redis=Redis.from_url(REDIS_URL)
MAX_VERIFY_RETRIES=3


def _now():return datetime.utcnow()


def _wake(generation_id):
    """Queue wake-up is best effort; the DB generation state is the source of truth."""
    try:redis.lpush('frontier:runs',f'gen:{generation_id}')
    except Exception:
        pass


def _observation_payload(db,benchmark_id):
    records=list_observations(db,benchmark_id)
    # Stable ordering is part of the fingerprint; list_observations already sorts by venue code.
    return records


def _snapshot_for(benchmark,records,inputs,outputs,sensitivity_count,algorithm):
    return pipeline.snapshot_payload({'id':benchmark.id,'code':benchmark.code,'period':benchmark.period},
                                     records,inputs,outputs,sensitivity_count,algorithm)


def _expected_fingerprints(payload):
    venue={r['observation_id']:pipeline.venue_fingerprint(payload,r) for r in payload['records']}
    return venue


def _parts_map(db,generation,stage):
    rows=db.scalars(select(GenerationPart).where(GenerationPart.generation_id==generation.id,
                                                 GenerationPart.stage==stage)).all()
    return {p.observation_id:p for p in rows}


def _part_dicts(parts):
    return {oid:{'status':p.status,'input_fingerprint':p.input_fingerprint,
                 'result':p.result,'error':p.error} for oid,p in parts.items()}


def get_generation(db,run_id,generation_id):
    gen=db.get(RunGeneration,generation_id)
    if not gen or gen.run_id!=run_id:raise HTTPException(404,'研究代不存在')
    return gen


def list_generations(db,run_id):
    if not db.get(EfficiencyRun,run_id):raise HTTPException(404,'研究不存在')
    gens=db.scalars(select(RunGeneration).where(RunGeneration.run_id==run_id)
                    .order_by(RunGeneration.generation_no)).all()
    return [_generation_summary(db,g) for g in gens]


def _generation_summary(db,gen):
    parts=db.scalars(select(GenerationPart).where(GenerationPart.generation_id==gen.id)).all()
    counts={}
    for p in parts:counts[p.status]=counts.get(p.status,0)+1
    return {'id':gen.id,'run_id':gen.run_id,'generation_no':gen.generation_no,
            'status':gen.status,'algorithm':gen.algorithm,'note':gen.note,
            'sensitivity_count':gen.sensitivity_count,
            'snapshot_fingerprint':gen.snapshot_fingerprint,
            'verification':gen.verification,'failure_scope':gen.failure_scope,
            'error':gen.error,'attempt':gen.attempt,
            'created_by':gen.created_by,'created_at':gen.created_at,
            'started_at':gen.started_at,'finished_at':gen.finished_at,
            'published_at':gen.published_at,
            'part_counts':counts,'part_total':len(parts),
            'completed_parts':counts.get('completed',0),
            'is_published':gen.status==GenerationStatus.PUBLISHED,
            'result_available':bool(gen.result) and gen.status==GenerationStatus.PUBLISHED}


def get_generation_detail(db,run_id,generation_id):
    gen=get_generation(db,run_id,generation_id)
    detail=_generation_summary(db,gen)
    run=db.get(EfficiencyRun,run_id)
    detail['published_generation_id']=run.published_generation_id
    detail['is_current']=run.published_generation_id==gen.id
    parts=db.scalars(select(GenerationPart).where(GenerationPart.generation_id==gen.id)
                     .order_by(GenerationPart.stage,GenerationPart.venue_code)).all()
    detail['parts']=[{'observation_id':p.observation_id,'venue_code':p.venue_code,
                      'venue_name':p.venue_name,'stage':p.stage,'status':p.status,
                      'input_fingerprint':p.input_fingerprint,'error':p.error,
                      'attempt':p.attempt,'completed_at':p.completed_at}
                     for p in parts]
    detail['snapshot']=gen.snapshot
    # A non-current generation exposes its result only for inspection after it has
    # published before; queued/failed drafts never leak as if they were usable data.
    detail['result']=gen.result if gen.result and gen.status in (
        GenerationStatus.READY,GenerationStatus.PUBLISHED,GenerationStatus.SUPERSEDED) else {}
    detail['selection']=gen.selection
    return detail


def _next_generation_no(db,run_id):
    numbers=db.scalars(select(RunGeneration.generation_no).where(RunGeneration.run_id==run_id)).all()
    return (max(numbers) if numbers else 0)+1


def create_generation(db,run_id,data,actor='',algorithm=DEFAULT_ALGORITHM,note=''):
    """Queue a new generation. The published result stays untouched until it verifies."""
    if algorithm not in SUPPORTED_ALGORITHMS:raise HTTPException(422,'不支持的算法版本')
    run=db.get(EfficiencyRun,run_id,with_for_update=True)
    if not run:raise HTTPException(404,'研究不存在')
    benchmark=db.get(BenchmarkSet,run.benchmark_id)
    if not benchmark:raise HTTPException(404,'参照集不存在')
    inputs=list(data.inputs if getattr(data,'inputs',None) else run.inputs)
    outputs=list(data.outputs if getattr(data,'outputs',None) else run.outputs)
    sensitivity_count=int(getattr(data,'sensitivity_count',None) or run.sensitivity_count)
    records=_observation_payload(db,benchmark.id)
    if len(records)<3:raise HTTPException(422,'至少需要3个同期场馆观察')
    payload=_snapshot_for(benchmark,records,inputs,outputs,sensitivity_count,algorithm)
    next_no=_next_generation_no(db,run_id)
    # A newer generation supersedes anything older that has not published yet.
    for older in db.scalars(select(RunGeneration).where(RunGeneration.run_id==run_id,
            RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING,
                                      GenerationStatus.READY]))).all():
        older.status=GenerationStatus.SUPERSEDED
        older.finished_at=older.finished_at or _now()
        older.error=older.error or f'已被第{next_no}代回算取代，未发布'
    gen=RunGeneration(run_id=run_id,generation_no=next_no,
                      status=GenerationStatus.QUEUED,algorithm=algorithm,
                      note=getattr(data,'note',None) or note,created_by=actor,
                      sensitivity_count=sensitivity_count,snapshot=payload,
                      snapshot_fingerprint=pipeline.snapshot_fingerprint(payload),
                      selection={},result={},verification={},failure_scope=[])
    db.add(gen);db.flush()
    for record in records:
        db.add(GenerationPart(generation_id=gen.id,observation_id=record['observation_id'],
                              venue_code=record['venue_code'],venue_name=record['venue_name'],
                              stage='baseline',status=GenerationPartStatus.PENDING))
    run.generation_count=gen.generation_no
    db.commit();_wake(gen.id)
    return gen


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _load_generation_for_execution(db,generation_id):
    gen=db.get(RunGeneration,generation_id)
    if not gen or gen.status not in ACTIVE_GENERATION_STATUSES:return None
    return gen


def _claim_run(db,gen):
    """Set queued->running exactly once; a superseded/failed generation is skipped."""
    changed=db.execute(update(RunGeneration)
                       .where(RunGeneration.id==gen.id,
                              RunGeneration.status.in_(ACTIVE_GENERATION_STATUSES))
                       .values(status=GenerationStatus.RUNNING,
                               started_at=gen.started_at or _now())).rowcount
    db.commit()
    return changed==1


def _ensure_part(db,gen,record,stage):
    """Idempotent part creation: a concurrent worker may have created it first."""
    part=db.scalar(select(GenerationPart).where(GenerationPart.generation_id==gen.id,
                                                 GenerationPart.observation_id==record['observation_id'],
                                                 GenerationPart.stage==stage))
    if part:return part
    part=GenerationPart(generation_id=gen.id,observation_id=record['observation_id'],
                        venue_code=record['venue_code'],venue_name=record['venue_name'],
                        stage=stage,status=GenerationPartStatus.PENDING)
    db.add(part)
    try:db.commit()
    except Exception:
        db.rollback()
        part=db.scalar(select(GenerationPart).where(GenerationPart.generation_id==gen.id,
                                                     GenerationPart.observation_id==record['observation_id'],
                                                     GenerationPart.stage==stage))
        if not part:raise
    return part


def _claim_part(db,part,wanted_fingerprint):
    """Atomic claim. Returns an attempt token, or None when a valid completed part
    already exists for this fingerprint (so resumed work never recomputes it)."""
    if part.status==GenerationPartStatus.COMPLETED and part.input_fingerprint==wanted_fingerprint:
        return None
    attempt=part.attempt+1
    changed=db.execute(update(GenerationPart)
                       .where(GenerationPart.id==part.id,
                              GenerationPart.attempt==part.attempt)
                       .values(status=GenerationPartStatus.RUNNING,attempt=attempt,
                               error='',started_at=_now(),completed_at=None,
                               input_fingerprint=wanted_fingerprint,result={})).rowcount
    db.commit()
    return attempt if changed==1 else None


def _complete_part(db,part_id,attempt,fingerprint,result,status=GenerationPartStatus.COMPLETED):
    """Guarded completion: a stale attempt (or a superseded generation reset) is ignored."""
    changed=db.execute(update(GenerationPart)
                       .where(GenerationPart.id==part_id,GenerationPart.attempt==attempt,
                              GenerationPart.status==GenerationPartStatus.RUNNING)
                       .values(status=status,result=result,input_fingerprint=fingerprint,
                               error='',completed_at=_now())).rowcount
    db.commit()
    return changed==1


def _fail_part(db,part_id,attempt,fingerprint,error):
    db.execute(update(GenerationPart)
               .where(GenerationPart.id==part_id,GenerationPart.attempt==attempt,
                      GenerationPart.status==GenerationPartStatus.RUNNING)
               .values(status=GenerationPartStatus.FAILED,error=error[:600],
                       input_fingerprint=fingerprint,completed_at=_now()))
    db.commit()


def _generation_still_running(db,generation_id):
    db.expire_all()
    gen=db.get(RunGeneration,generation_id)
    return bool(gen and gen.status==GenerationStatus.RUNNING)


def execute_generation(db,generation_id,stop_after_baseline=False):
    """Compute all missing parts. Safe to call repeatedly (resume semantics)."""
    gen=_load_generation_for_execution(db,generation_id)
    if not gen:return False
    payload=gen.snapshot
    if not payload or not payload.get('records'):
        _fail_generation(db,gen,[], '冻结快照为空，无法执行')
        return False
    if not _claim_run(db,gen):return False
    db.refresh(gen)
    records=payload['records']
    baseline_parts=_parts_map(db,gen,'baseline')
    # 1) Baseline part per venue.
    for record in records:
        if not _generation_still_running(db,gen.id):
            return False
        part=_ensure_part(db,gen,record,'baseline')
        fingerprint=pipeline.venue_fingerprint(payload,record)
        attempt=_claim_part(db,part,fingerprint)
        if attempt is None:continue
        try:
            result=pipeline.solve_baseline(payload,record['observation_id'])
            _complete_part(db,part.id,attempt,fingerprint,result)
        except Exception as exc:
            _record_part_failure(db,gen,part,attempt,fingerprint,record,'baseline',exc)
    if stop_after_baseline:return True
    # 2) Deterministic selection from completed baselines.
    baseline_parts=_parts_map(db,gen,'baseline')
    complete=[p for p in baseline_parts.values() if p.status==GenerationPartStatus.COMPLETED]
    if len(complete)!=len(records):return False
    baseline_results=[baseline_parts[r['observation_id']].result for r in records]
    try:
        selection=pipeline.select_benchmarks(payload,baseline_results,gen.sensitivity_count)
    except Exception as exc:
        _fail_generation(db,gen,[],f'标杆选择失败：{exc}')
        return False
    db.execute(update(RunGeneration).where(RunGeneration.id==gen.id)
               .values(selection=selection))
    db.commit();db.refresh(gen)
    # 3) Sensitivity part per target.
    for record in records:
        if not _generation_still_running(db,gen.id):
            return False
        part=_ensure_part(db,gen,record,'sensitivity')
        fingerprint=pipeline.sensitivity_fingerprint(payload,record,selection)
        attempt=_claim_part(db,part,fingerprint)
        if attempt is None:continue
        try:
            result=pipeline.solve_sensitivity(payload,record['observation_id'],selection,
                                              baseline_parts[record['observation_id']].result)
            _complete_part(db,part.id,attempt,fingerprint,result)
        except Exception as exc:
            _record_part_failure(db,gen,part,attempt,fingerprint,record,'sensitivity',exc)
    # 4) Verify and publish.
    return finalize_generation(db,gen.id)


def _record_part_failure(db,gen,part,attempt,fingerprint,record,stage,exc):
    _fail_part(db,part.id,attempt,fingerprint,str(exc))
    db.expire_all()
    current=db.get(RunGeneration,gen.id)
    if not current or current.status!=GenerationStatus.RUNNING:return False
    scope=list(current.failure_scope or [])
    scope.append({'observation_id':record['observation_id'],'venue_code':record['venue_code'],
                  'venue_name':record['venue_name'],'stage':stage,'error':str(exc)[:600]})
    db.execute(update(RunGeneration).where(RunGeneration.id==gen.id,
                                           RunGeneration.status==GenerationStatus.RUNNING).values(
        status=GenerationStatus.FAILED,error=f"{record['venue_code']} {stage} 计算失败：{str(exc)[:400]}",
        failure_scope=scope,finished_at=_now()))
    db.commit()
    return False


def _fail_generation(db,gen,scope,error):
    db.execute(update(RunGeneration).where(RunGeneration.id==gen.id,
        RunGeneration.status.in_([GenerationStatus.RUNNING,GenerationStatus.READY])).values(
        status=GenerationStatus.FAILED,error=str(error)[:600],failure_scope=scope,
        finished_at=_now()))
    db.commit()


def finalize_generation(db,generation_id):
    """Verify every venue part; publish atomically only when all checks pass."""
    db.expire_all()
    gen=db.get(RunGeneration,generation_id)
    if not gen or gen.status not in (GenerationStatus.RUNNING,GenerationStatus.READY):
        return False
    run=db.get(EfficiencyRun,gen.run_id)
    payload=gen.snapshot;selection=gen.selection or {}
    baseline_parts=_parts_map(db,gen,'baseline');sensitivity_parts=_parts_map(db,gen,'sensitivity')
    # One baseline part and one sensitivity part per target venue.
    expected=len(payload['records'])
    if (sum(p.status==GenerationPartStatus.COMPLETED for p in baseline_parts.values())<expected or
            sum(p.status==GenerationPartStatus.COMPLETED for p in sensitivity_parts.values())<expected):
        return False
    verification=pipeline.verify_generation(payload,selection,_part_dicts(baseline_parts),
                                            _part_dicts(sensitivity_parts))
    if not verification['ok']:
        scope=[{'observation_id':issue['observation_id'],'venue_code':issue['venue_code'],
                'stage':'verification','check':issue['check'],'error':issue['detail']}
               for issue in verification['issues']]
        _fail_generation(db,gen,scope,'逐馆核对未通过，第%s代未发布'%gen.generation_no)
        return False
    try:
        result=pipeline.assemble_result(payload,selection,_part_dicts(baseline_parts),
                                        _part_dicts(sensitivity_parts),
                                        (db.get(BenchmarkSet,run.benchmark_id).name))
    except Exception as exc:
        _fail_generation(db,gen,[],f'汇总失败：{exc}')
        return False
    db.execute(update(RunGeneration).where(RunGeneration.id==gen.id,
                                           RunGeneration.status==GenerationStatus.RUNNING).values(
        status=GenerationStatus.READY,result=result,verification=verification,
        failure_scope=[],error='',finished_at=_now()))
    db.commit()
    return publish_generation(db,gen.id)


def publish_generation(db,generation_id):
    """Atomic publish. One compare-and-swap statement decides the winner.

    The run row is first locked (PostgreSQL FOR UPDATE) and the generation must be
    READY and the newest created for the run. The result swap itself is a single
    conditional UPDATE on a monotonic `published_generation_no`, so two concurrent
    publishers, a replay, or an out-of-order completion can never overwrite a newer
    published generation. Losers are marked superseded.
    """
    gen=db.get(RunGeneration,generation_id)
    if not gen:raise HTTPException(404,'研究代不存在')
    run=db.get(EfficiencyRun,gen.run_id,with_for_update=True)
    db.refresh(gen)
    if run.published_generation_id==gen.id:
        if gen.status!=GenerationStatus.PUBLISHED:
            gen.status=GenerationStatus.PUBLISHED;gen.published_at=gen.published_at or _now();db.commit()
        return True
    if gen.status==GenerationStatus.SUPERSEDED:return False
    if gen.status==GenerationStatus.PUBLISHED:
        # Published marker but not the run's current pointer: never re-promote an older代.
        return False
    if gen.status!=GenerationStatus.READY:
        raise HTTPException(409,f'第{gen.generation_no}代尚未通过逐馆核对，不能发布')
    newest=db.scalar(select(RunGeneration.generation_no).where(RunGeneration.run_id==run.id)
                     .order_by(RunGeneration.generation_no.desc()))
    if newest is not None and gen.generation_no<newest:
        db.execute(update(RunGeneration).where(RunGeneration.id==gen.id).values(
            status=GenerationStatus.SUPERSEDED,
            error=f'第{newest}代已建立，第{gen.generation_no}代晚到，改为取代状态，未覆盖当前结果',
            finished_at=gen.finished_at or _now()))
        db.commit();return False
    changed=db.execute(update(EfficiencyRun)
        .where(EfficiencyRun.id==run.id,
               (EfficiencyRun.published_generation_no.is_(None)) |
               (EfficiencyRun.published_generation_no<gen.generation_no))
        .values(result=gen.result,status=RunStatus.COMPLETED,error='',
                published_generation_id=gen.id,
                published_generation_no=gen.generation_no)).rowcount
    if changed!=1:
        # A newer generation published concurrently; this one must not overwrite it.
        db.rollback()
        db.execute(update(RunGeneration).where(RunGeneration.id==gen.id).values(
            status=GenerationStatus.SUPERSEDED,
            error=f'并发发布时已有更新的研究代，第{gen.generation_no}代未覆盖当前结果',
            finished_at=_now()))
        db.commit();return False
    # The swap bypassed the ORM identity map; make the in-session row reflect it.
    db.expire(run)
    db.execute(update(RunGeneration).where(RunGeneration.id==gen.id).values(
        status=GenerationStatus.PUBLISHED,published_at=_now(),
        finished_at=gen.finished_at or _now()))
    # Any older unpublished generation is stale by definition.
    for older in db.scalars(select(RunGeneration).where(RunGeneration.run_id==run.id,
            RunGeneration.generation_no<gen.generation_no,
            RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING,
                                      GenerationStatus.READY]))).all():
        older.status=GenerationStatus.SUPERSEDED
        older.error=older.error or f'第{gen.generation_no}代已发布，本代未发布'
        older.finished_at=older.finished_at or _now()
    db.commit()
    return True


def retry_generation(db,run_id,generation_id,actor=''):
    """Resume a failed generation: only missing/failed parts are recomputed."""
    gen=get_generation(db,run_id,generation_id)
    if gen.status not in (GenerationStatus.FAILED,GenerationStatus.SUPERSEDED):
        raise HTTPException(409,'仅失败或已取代的研究代可以补算')
    newer=db.scalar(select(RunGeneration.generation_no).where(RunGeneration.run_id==run_id,
                     RunGeneration.generation_no>gen.generation_no))
    if newer:
        raise HTTPException(409,f'已有更新的第{newer}代，不能再补算第{gen.generation_no}代')
    scope={(item.get('observation_id'),item.get('stage')) for item in (gen.failure_scope or [])}
    scope_ids={item.get('observation_id') for item in (gen.failure_scope or []) if item.get('observation_id')}
    payload=gen.snapshot or {};records=payload.get('records',[])
    venue_fps=_expected_fingerprints(payload) if records else {}
    baseline_parts=_parts_map(db,gen,'baseline');sensitivity_parts=_parts_map(db,gen,'sensitivity')
    changed=0
    for stage,parts in (('baseline',baseline_parts),('sensitivity',sensitivity_parts)):
        for record in records:
            part=parts.get(record['observation_id'])
            if not part:continue
            expected=(venue_fps.get(record['observation_id']) if stage=='baseline'
                      else pipeline.sensitivity_fingerprint(payload,record,gen.selection or {}))
            stale=(part.status!=GenerationPartStatus.COMPLETED or part.input_fingerprint!=expected
                   or (record['observation_id'],stage) in scope
                   or (record['observation_id'] in scope_ids and stage=='sensitivity'))
            if stale:
                part.status=GenerationPartStatus.PENDING;part.result={};part.error=''
                part.completed_at=None;part.attempt=part.attempt+1;changed+=1
    gen.status=GenerationStatus.QUEUED;gen.error='';gen.failure_scope=[]
    gen.verification={};gen.finished_at=None;gen.attempt=gen.attempt+1
    # A retry that starts before any newer publish must re-verify selection deterministically.
    if gen.selection and payload.get('records'):
        baseline_parts=_parts_map(db,gen,'baseline')
        if all(p.status==GenerationPartStatus.COMPLETED for p in baseline_parts.values()):
            try:
                gen.selection=pipeline.select_benchmarks(payload,
                    [baseline_parts[r['observation_id']].result for r in records],gen.sensitivity_count)
            except Exception:
                pass
    db.commit()
    _wake(gen.id)
    return gen


def adopt_legacy_runs(db):
    """Give pre-upgrade completed runs a synthetic published generation (idempotent).

    This keeps existing research and improvement cases readable and makes the
    first real recompute a normal generation 2.
    """
    adopted=[]
    runs=db.scalars(select(EfficiencyRun).where(EfficiencyRun.published_generation_id.is_(None))).all()
    for run in runs:
        if not run.result or not run.result.get('baseline'):continue
        if db.scalar(select(RunGeneration.id).where(RunGeneration.run_id==run.id,
                                                     RunGeneration.generation_no==1)):continue
        benchmark=db.get(BenchmarkSet,run.benchmark_id)
        if not benchmark:continue
        try:records=_observation_payload(db,benchmark.id)
        except Exception:continue
        payload=pipeline.snapshot_payload({'id':benchmark.id,'code':benchmark.code,'period':benchmark.period},
                                          records,run.inputs,run.outputs,run.sensitivity_count,
                                          getattr(run,'algorithm',None) or DEFAULT_ALGORITHM)
        result=dict(run.result)
        result.setdefault('fingerprint',pipeline.snapshot_fingerprint(payload))
        result.setdefault('period',benchmark.period);result.setdefault('benchmark_name',benchmark.name)
        gen=RunGeneration(run_id=run.id,generation_no=1,status=GenerationStatus.PUBLISHED,
                          algorithm=getattr(run,'algorithm',None) or DEFAULT_ALGORITHM,
                          note='升级前的已完成研究，作为第1代基线登记',sensitivity_count=run.sensitivity_count,
                          snapshot=payload,snapshot_fingerprint=pipeline.snapshot_fingerprint(payload),
                          selection={'policy':pipeline.SENSITIVITY_POLICY,
                                     'sensitivity_count':run.sensitivity_count,
                                     'legacy':True,
                                     'selected':[{'observation_id':s['removed_observation_id'],
                                                  'venue_code':s['removed_code'],
                                                  'venue_name':s['removed_name'],
                                                  'aggregate_external_lambda':s['aggregate_external_lambda']}
                                                 for s in result.get('scenarios',[])]},
                          result=result,published_at=run.created_at,finished_at=run.created_at,
                          created_at=run.created_at,created_by='migration')
        db.add(gen);db.flush()
        for row in result.get('baseline',[]):
            db.add(GenerationPart(generation_id=gen.id,observation_id=row['observation_id'],
                                  venue_code=row['venue_code'],venue_name=row['venue_name'],
                                  stage='baseline',status=GenerationPartStatus.COMPLETED,
                                  input_fingerprint=pipeline.venue_fingerprint(payload,
                                      next(r for r in records if r['observation_id']==row['observation_id'])),
                                  result=row,completed_at=run.created_at))
        for row in result.get('baseline',[]):
            scenarios=[]
            for scenario in result.get('scenarios',[]):
                score=next((s for s in scenario.get('scores',[])
                            if s['observation_id']==row['observation_id']),None)
                if score:scenarios.append({'removed_observation_id':scenario['removed_observation_id'],
                                           'status':score['status'],'theta':score['theta'],
                                           'delta':score['delta'],
                                           'removed_target_self':score['removed_target_self']})
            db.add(GenerationPart(generation_id=gen.id,observation_id=row['observation_id'],
                                  venue_code=row['venue_code'],venue_name=row['venue_name'],
                                  stage='sensitivity',status=GenerationPartStatus.COMPLETED,
                                  input_fingerprint=pipeline.sensitivity_fingerprint(payload,
                                      next(r for r in records if r['observation_id']==row['observation_id']),
                                      gen.selection),
                                  result={'observation_id':row['observation_id'],
                                          'venue_code':row['venue_code'],'base_theta':row['theta'],
                                          'scenarios':scenarios},completed_at=run.created_at))
        run.published_generation_id=gen.id;run.published_generation_no=1;run.generation_count=1
        if run.status==RunStatus.FAILED:run.status=RunStatus.COMPLETED
        adopted.append(run.id)
    if adopted:db.commit()
    return adopted


def generation_diff(db,run_id,from_id=None,to_id=None):
    """Per-venue difference between two generations (defaults: previous vs current)."""
    run=db.get(EfficiencyRun,run_id)
    if not run:raise HTTPException(404,'研究不存在')
    gens=db.scalars(select(RunGeneration).where(RunGeneration.run_id==run_id)
                    .order_by(RunGeneration.generation_no)).all()
    if len(gens)<1:raise HTTPException(404,'尚无研究代')
    published=[g for g in gens if g.status==GenerationStatus.PUBLISHED]
    to=db.get(RunGeneration,to_id) if to_id else (published[-1] if published else gens[-1])
    if not to or to.run_id!=run_id:raise HTTPException(404,'研究代不存在')
    if from_id:
        frm=db.get(RunGeneration,from_id)
        if not frm or frm.run_id!=run_id:raise HTTPException(404,'对照研究代不存在')
    else:
        earlier=[g for g in gens if g.generation_no<to.generation_no]
        frm=earlier[-1] if earlier else None
    def rows(gen):
        if not gen or not gen.result:return {}
        return {r['observation_id']:r for r in gen.result.get('baseline',[])}
    before,after_rows=rows(frm),rows(to)
    venues=[]
    for observation_id,row in after_rows.items():
        prior=before.get(observation_id)
        venues.append({'observation_id':observation_id,'venue_code':row['venue_code'],
                       'venue_name':row['venue_name'],
                       'theta_before':prior['theta'] if prior else None,
                       'theta_after':row['theta'],
                       'delta':None if not prior else row['theta']-prior['theta'],
                       'status_before':'missing' if not prior else 'optimal',
                       'status_after':row.get('status','optimal')})
    return {'run_id':run_id,
            'from':None if not frm else {'id':frm.id,'generation_no':frm.generation_no,
                                         'status':frm.status,'fingerprint':frm.snapshot_fingerprint,
                                         'published_at':frm.published_at},
            'to':{'id':to.id,'generation_no':to.generation_no,'status':to.status,
                  'fingerprint':to.snapshot_fingerprint,'published_at':to.published_at,
                  'verification':to.verification,'failure_scope':to.failure_scope},
            'published_generation_id':run.published_generation_id,
            'current_generation_id':run.published_generation_id,
            'sample_changed':bool(frm and frm.snapshot_fingerprint!=to.snapshot_fingerprint),
            'venues':venues,
            'only_in_after':sorted(set(after_rows)-set(before)),
            'only_in_before':sorted(set(before)-set(after_rows))}
