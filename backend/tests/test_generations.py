import json
import pytest
from sqlalchemy import select
from conftest import build_db,seed_sample
from src.models import EfficiencyRun,RunGeneration,GenerationPart,ImprovementCase
from src.constants.enums import GenerationStatus,GenerationPartStatus
from src.schemas.run import RunInput,GenerationInput
from src.services import generation_service as gs
from src.services import dea_pipeline as pipeline
from src.services.run_service import create_run

pytestmark=pytest.mark.usefixtures('ctx')

@pytest.fixture()
def ctx(tmp_path):
    engine,Session=build_db(str(tmp_path/'test.db'))
    benchmark_id=seed_sample(Session)
    return {'engine':engine,'Session':Session,'benchmark_id':benchmark_id}


def _new_run(ctx,name='测试研究',sensitivity_count=2):
    with ctx['Session']() as db:
        run=create_run(db,RunInput(name=name,benchmark_id=ctx['benchmark_id'],
                                   inputs=['staff_hours','energy_cost'],
                                   outputs=['visits','programs'],
                                   sensitivity_count=sensitivity_count),actor='analyst@x')
    return run


def _execute(ctx,generation_id,**kwargs):
    with ctx['Session']() as db:
        return gs.execute_generation(db,generation_id,**kwargs)


def _run_row(ctx,run_id):
    with ctx['Session']() as db:
        return db.get(EfficiencyRun,run_id)


def _gen_row(ctx,generation_id):
    with ctx['Session']() as db:
        return db.get(RunGeneration,generation_id)


def _parts(ctx,generation_id,stage=None):
    with ctx['Session']() as db:
        q=select(GenerationPart).where(GenerationPart.generation_id==generation_id)
        if stage:q=q.where(GenerationPart.stage==stage)
        return list(db.scalars(q.order_by(GenerationPart.observation_id)).all())


def _publish_new_generation(ctx,run_id):
    with ctx['Session']() as db:
        gen=gs.create_generation(db,run_id,GenerationInput(sensitivity_count=2),actor='analyst@x')
        return gen.id,gen.generation_no


def test_first_generation_publishes_and_history_is_empty(ctx):
    run=_new_run(ctx)
    assert run['generation_no']==1
    assert _execute(ctx,run['generation_id']) is True
    row=_run_row(ctx,run['id'])
    assert row.status=='completed'
    assert row.published_generation_id==run['generation_id']
    assert row.result['n']==12
    gen=_gen_row(ctx,run['generation_id'])
    assert gen.status==GenerationStatus.PUBLISHED
    assert gen.verification['ok'] is True
    assert gen.verification['checked_venues']==12
    baseline=_parts(ctx,run['generation_id'],'baseline')
    sens=_parts(ctx,run['generation_id'],'sensitivity')
    assert all(p.status==GenerationPartStatus.COMPLETED for p in baseline+sens)
    assert all(p.input_fingerprint for p in baseline+sens)


def test_new_generation_keeps_old_result_visible_until_published(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    with ctx['Session']() as db:
        old_fingerprint=db.get(EfficiencyRun,run['id']).result['fingerprint']
    gen2,no=_publish_new_generation(ctx,run['id'])
    assert no==2
    # Queued generation: public result untouched, status still completed.
    row=_run_row(ctx,run['id'])
    assert row.result['fingerprint']==old_fingerprint
    assert row.status=='completed'
    assert row.published_generation_id==run['generation_id']
    assert _execute(ctx,gen2) is True
    row=_run_row(ctx,run['id'])
    assert row.published_generation_id==gen2
    assert row.result['fingerprint']==_gen_row(ctx,gen2).result['fingerprint']
    assert _gen_row(ctx,run['generation_id']).status==GenerationStatus.PUBLISHED


def test_interrupted_generation_resumes_only_missing_parts(ctx,monkeypatch):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    gen2,no=_publish_new_generation(ctx,run['id'])
    # Stop right after the baseline stage, simulating a worker crash.
    assert _execute(ctx,gen2,stop_after_baseline=True) is True
    baseline=_parts(ctx,gen2,'baseline');sens=_parts(ctx,gen2,'sensitivity')
    assert all(p.status==GenerationPartStatus.COMPLETED for p in baseline)
    assert all(p.status==GenerationPartStatus.PENDING for p in sens)
    # A second generation supersedes gen2 before it finishes.
    gen3,no3=_publish_new_generation(ctx,run['id'])
    assert no3==3
    assert _gen_row(ctx,gen2).status==GenerationStatus.SUPERSEDED
    # Resuming the superseded generation must not publish or overwrite the run.
    with ctx['Session']() as db:
        assert gs.execute_generation(db,gen2) is False
    assert _run_row(ctx,run['id']).published_generation_id==run['generation_id']
    assert _gen_row(ctx,gen3).status==GenerationStatus.QUEUED
    assert _execute(ctx,gen3) is True
    assert _run_row(ctx,run['id']).published_generation_id==gen3


def test_retry_recomputes_only_failed_venues(ctx,monkeypatch):
    run=_new_run(ctx)
    calls={'baseline':[],'sensitivity':[]}
    real_baseline=pipeline.solve_baseline
    real_sensitivity=pipeline.solve_sensitivity
    broken={'venue':'V-05','stage':'baseline'}

    def flaky_baseline(payload,observation_id):
        record=next(r for r in payload['records'] if r['observation_id']==observation_id)
        calls['baseline'].append(record['venue_code'])
        if broken['stage']=='baseline' and record['venue_code']==broken['venue']:
            raise RuntimeError('模拟求解器中断')
        return real_baseline(payload,observation_id)

    def flaky_sensitivity(payload,observation_id,selection,baseline_result):
        record=next(r for r in payload['records'] if r['observation_id']==observation_id)
        calls['sensitivity'].append(record['venue_code'])
        if broken['stage']=='sensitivity' and record['venue_code']==broken['venue']:
            raise RuntimeError('模拟敏感性中断')
        return real_sensitivity(payload,observation_id,selection,baseline_result)

    monkeypatch.setattr(gs.pipeline,'solve_baseline',flaky_baseline)
    monkeypatch.setattr(gs.pipeline,'solve_sensitivity',flaky_sensitivity)
    assert _execute(ctx,run['generation_id']) is False
    gen=_gen_row(ctx,run['generation_id'])
    assert gen.status==GenerationStatus.FAILED
    assert [f['venue_code'] for f in gen.failure_scope]==['V-05']
    # Nothing is published; the run remains queued for the first generation.
    assert _run_row(ctx,run['id']).published_generation_id is None
    # Retry after fixing the solver: completed venues are not recomputed, only the
    # failed and never-started parts are.
    done={p.venue_code for p in _parts(ctx,run['generation_id'],'baseline')
          if p.status==GenerationPartStatus.COMPLETED}
    calls['baseline'].clear();calls['sensitivity'].clear()
    broken['stage']=None
    with ctx['Session']() as db:
        retry=gs.retry_generation(db,run['id'],gen.id,actor='analyst@x')
    assert retry.status==GenerationStatus.QUEUED
    assert _execute(ctx,gen.id) is True
    # Venues that finished on the first attempt are never recomputed; the work
    # covers the failed venue and the parts that never started.
    assert set(calls['baseline']).isdisjoint(done)
    assert calls['baseline'] and calls['baseline'][0]=='V-05'
    assert _run_row(ctx,run['id']).published_generation_id==gen.id
    # Verification re-derives every target's baseline theta from the frozen sample.
    assert _gen_row(ctx,gen.id).verification['checked_venues']==12
    assert _gen_row(ctx,gen.id).attempt==1


def test_failure_after_old_published_keeps_old_result(ctx,monkeypatch):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    old=_run_row(ctx,run['id'])
    old_fingerprint=old.result['fingerprint']
    gen2,no=_publish_new_generation(ctx,run['id'])
    real=pipeline.solve_baseline
    def boom(payload,observation_id):
        record=next(r for r in payload['records'] if r['observation_id']==observation_id)
        if record['venue_code']=='V-03':raise RuntimeError('晚到任务失败')
        return real(payload,observation_id)
    monkeypatch.setattr(gs.pipeline,'solve_baseline',boom)
    assert _execute(ctx,gen2) is False
    row=_run_row(ctx,run['id'])
    assert row.result['fingerprint']==old_fingerprint
    assert row.published_generation_id==run['generation_id']
    assert row.status=='completed'


def test_verification_failure_blocks_publish(ctx):
    run=_new_run(ctx)
    # Finish only the baseline stage, then simulate a corrupted stored score.
    assert _execute(ctx,run['generation_id'],stop_after_baseline=True) is True
    target=next(p for p in _parts(ctx,run['generation_id'],'baseline') if p.venue_code=='V-04')
    with ctx['Session']() as db:
        part=db.get(GenerationPart,target.id)
        tampered=dict(part.result);tampered['theta']=float(tampered['theta'])*0.5
        part.result=tampered;db.commit()
    assert _execute(ctx,run['generation_id']) is False
    gen=_gen_row(ctx,run['generation_id'])
    assert gen.status==GenerationStatus.FAILED
    assert any(f.get('check')=='baseline_theta_recomputed' and f['venue_code']=='V-04'
               for f in gen.failure_scope)
    assert _run_row(ctx,run['id']).published_generation_id is None


def test_out_of_order_publish_cannot_overwrite_current(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    gen2,_=_publish_new_generation(ctx,run['id'])
    gen3,_=_publish_new_generation(ctx,run['id'])
    # Simulate a worker that was mid-flight on gen2 and completes it after gen3 exists.
    with ctx['Session']() as db:
        gen=db.get(RunGeneration,gen2)
        gen.status=GenerationStatus.READY
        gen.result={'baseline':[{'observation_id':1,'venue_code':'V-01','venue_name':'场馆01',
                                 'theta':0.99,'status':'optimal'}]}
        db.commit()
    assert _gen_row(ctx,gen2).status==GenerationStatus.READY
    with ctx['Session']() as db:
        assert gs.publish_generation(db,gen2) is False
    assert _gen_row(ctx,gen2).status==GenerationStatus.SUPERSEDED
    # gen3 publishes normally; gen2's late publish never changed the run.
    assert _execute(ctx,gen3) is True
    row=_run_row(ctx,run['id'])
    assert row.published_generation_id==gen3
    assert row.result['n']==12
    # A second late publish attempt still cannot overwrite.
    with ctx['Session']() as db:
        assert gs.publish_generation(db,gen2) is False
    assert _run_row(ctx,run['id']).published_generation_id==gen3


def test_replay_of_published_generation_is_idempotent(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    with ctx['Session']() as db:
        before=db.get(RunGeneration,run['generation_id']).result['fingerprint']
        assert gs.publish_generation(db,run['generation_id']) is True
        assert gs.publish_generation(db,run['generation_id']) is True
    with ctx['Session']() as db:
        after=db.get(EfficiencyRun,run['id']).result['fingerprint']
    assert before==after


def test_stale_part_attempt_cannot_overwrite(ctx):
    run=_new_run(ctx)
    gen_id=run['generation_id']
    part=_parts(ctx,gen_id,'baseline')[0]
    with ctx['Session']() as db:
        fresh=db.get(GenerationPart,part.id)
        attempt=gs._claim_part(db,fresh,fresh.input_fingerprint or 'fp-a')
    assert attempt==1
    with ctx['Session']() as db:
        stale=db.get(GenerationPart,part.id)
        # A newer attempt claims the part first.
        second=gs._claim_part(db,stale,fresh.input_fingerprint or 'fp-a')
    assert second==2
    # The old worker finish must be ignored.
    with ctx['Session']() as db:
        assert gs._complete_part(db,part.id,attempt,'fp-a',{'status':'optimal','theta':0.1}) is False
    current=[p for p in _parts(ctx,gen_id,'baseline') if p.id==part.id][0]
    assert current.status==GenerationPartStatus.RUNNING
    assert current.result=={}


def test_generation_diff_and_failure_scope_visibility(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    gen2,no=_publish_new_generation(ctx,run['id'])
    _execute(ctx,gen2)
    with ctx['Session']() as db:
        diff=gs.generation_diff(db,run['id'])
    assert diff['from']['generation_no']==1
    assert diff['to']['generation_no']==2
    assert len(diff['venues'])==12
    assert all(v['delta'] is not None for v in diff['venues'])
    assert diff['to']['verification']['ok'] is True


def test_case_pins_source_generation_and_migrates(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    with ctx['Session']() as db:
        row=db.get(EfficiencyRun,run['id']).result['baseline'][3]
        observation_id=row['observation_id'];old_theta=row['theta']
    from src.schemas.case import CaseInput
    from src.services.case_service import create_case,export_case,migrate_case,list_cases
    with ctx['Session']() as db:
        case=create_case(db,CaseInput(title='现场复核假设',run_id=run['id'],observation_id=observation_id,
                                      hypothesis='核对统计边界后再讨论投入缩减空间。',
                                      verification='两周现场观察并复核口径。'))
        case_id=case.id
        assert case.source_generation_id==run['generation_id']
        assert case.source_evidence['theta']==old_theta
    gen2,no=_publish_new_generation(ctx,run['id']);_execute(ctx,gen2)
    with ctx['Session']() as db:
        cases=list_cases(db)
        assert cases[0]['is_generation_current'] is False
        assert cases[0]['source_generation_no']==1
        # The frozen evidence still explains the hypothesis after generation 2 publishes.
        assert cases[0]['source_evidence']['theta']==old_theta
    with ctx['Session']() as db:
        migrated=migrate_case(db,case_id,gen2,type('U',(),{'email':'analyst@x'})())
        assert migrated.source_generation_id==gen2
        assert len(migrated.migration_history)==1
        assert migrated.migration_history[0]['from']['theta']==old_theta
        assert migrated.migration_history[0]['to']['generation_no']==2
        assert migrated.source_evidence['generation_no']==2
        migrated.status='approved';migrated.approved_by='admin@x';db.commit()
    with ctx['Session']() as db:
        csv_text=export_case(db,case_id)
    header=csv_text.splitlines()[0]
    assert 'source_generation_no' in header and 'current_generation_no' in header
    assert str(old_theta) in csv_text


def test_legacy_run_is_adopted_as_generation_one(ctx):
    run=_new_run(ctx);_execute(ctx,run['generation_id'])
    legacy_name='升级前研究'
    with ctx['Session']() as db:
        original=db.get(EfficiencyRun,run['id'])
        snapshot=json.loads(json.dumps(original.result))
        legacy=EfficiencyRun(name=legacy_name,benchmark_id=original.benchmark_id,
                             inputs=original.inputs,outputs=original.outputs,
                             sensitivity_count=original.sensitivity_count,
                             status='completed',result=snapshot)
        db.add(legacy);db.commit();legacy_id=legacy.id
        assert gs.adopt_legacy_runs(db)==[legacy_id]
        adopted=db.get(EfficiencyRun,legacy_id)
        gen=db.get(RunGeneration,adopted.published_generation_id)
        assert gen.generation_no==1 and gen.status==GenerationStatus.PUBLISHED
        assert gen.result['fingerprint']==adopted.result['fingerprint']
        # Idempotent: a second call adopts nothing.
        assert gs.adopt_legacy_runs(db)==[]
    # The first real recompute is a normal generation 2.
    gen2,no=_publish_new_generation(ctx,legacy_id)
    assert no==2
    assert _execute(ctx,gen2) is True
    assert _run_row(ctx,legacy_id).published_generation_id==gen2
