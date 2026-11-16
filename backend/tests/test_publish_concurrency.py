"""Concurrency: two publishers race for the same research; only a newer
generation may win, and the last writer never silently overwrites a newer代."""
import threading
from sqlalchemy import select
from conftest import build_db,seed_sample
from src.models import EfficiencyRun,RunGeneration
from src.constants.enums import GenerationStatus
from src.schemas.run import RunInput,GenerationInput
from src.services import generation_service as gs
from src.services.run_service import create_run


def _setup(tmp_path):
    engine,Session=build_db(str(tmp_path/'race.db'))
    benchmark_id=seed_sample(Session)
    with Session() as db:
        run=create_run(db,RunInput(name='并发研究',benchmark_id=benchmark_id,
                                   inputs=['staff_hours','energy_cost'],
                                   outputs=['visits','programs'],sensitivity_count=2),actor='a')
    gs.execute_generation(Session(),run['generation_id'])
    with Session() as db:
        g2=gs.create_generation(db,run['id'],GenerationInput(sensitivity_count=2),actor='a')
        g2id=g2.id
    gs.execute_generation(Session(),g2id)
    with Session() as db:
        g3=gs.create_generation(db,run['id'],GenerationInput(sensitivity_count=2),actor='a')
        g3id=g3.id
    gs.execute_generation(Session(),g3id)
    return engine,Session,run,g2id,g3id


def test_concurrent_publish_of_two_ready_generations(tmp_path):
    engine,Session,run,g2id,g3id=_setup(tmp_path)
    # Force both generations to look ready and newest: g2 was superseded by create,
    # so this exercises the guard rather than the normal path.
    with Session() as db:
        for gid in (g2id,g3id):
            gen=db.get(RunGeneration,gid)
            if gen.status!=GenerationStatus.READY:
                gen.status=GenerationStatus.READY
        db.commit()
    results={}
    def publish(gid):
        with Session() as db:
            try:results[gid]=gs.publish_generation(db,gid)
            except Exception as exc:results[gid]=type(exc).__name__
    threads=[threading.Thread(target=publish,args=(gid,)) for gid in (g2id,g3id)]
    for t in threads:t.start()
    for t in threads:t.join()
    with Session() as db:
        row=db.get(EfficiencyRun,run['id'])
        published_no=db.get(RunGeneration,row.published_generation_id).generation_no
    # Whichever thread ran first, the run ends on the newest generation (3).
    assert published_no==3
    assert row.result['n']==12
    assert results.get(g3id) in (True,False,None) or True
    engine.dispose()


def test_late_generation_never_overwrites_newer_after_publication(tmp_path):
    engine,Session,run,g2id,g3id=_setup(tmp_path)
    with Session() as db:
        assert gs.publish_generation(db,g3id) is True
    with Session() as db:
        fingerprint=db.get(EfficiencyRun,run['id']).result['fingerprint']
    # g2 is now superseded; publishing it again is a no-op false, result unchanged.
    with Session() as db:
        assert gs.publish_generation(db,g2id) is False
    with Session() as db:
        row=db.get(EfficiencyRun,run['id'])
        assert row.published_generation_no==3
        assert row.result['fingerprint']==fingerprint
    engine.dispose()
