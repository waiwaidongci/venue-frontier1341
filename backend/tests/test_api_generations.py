"""API-level checks with an isolated SQLite database and a stub queue."""
import pytest
from fastapi.testclient import TestClient
import src.services.generation_service as generation_service
from src.services.run_service import execute_run
from conftest import build_db,seed_sample


class _Queue:
    def lpush(self,*a,**k):return 1
    def brpop(self,*a,**k):return None
    def incr(self,*a,**k):return 1
    def expire(self,*a,**k):return True


@pytest.fixture()
def client(tmp_path,monkeypatch):
    engine,Session=build_db(str(tmp_path/'api.db'))
    benchmark_id=seed_sample(Session)
    monkeypatch.setattr(generation_service,'redis',_Queue())
    import src.config.database as database
    monkeypatch.setattr(database,'Session',Session)
    monkeypatch.setattr(database,'engine',engine)
    import src.main as main
    monkeypatch.setattr(main,'engine',engine)
    monkeypatch.setattr(main,'SEED_DEMO',False)
    import src.middlewares.rate_limit as rate_limit
    class _Factory:
        @staticmethod
        def from_url(*a,**k):return _Queue()
    monkeypatch.setattr(rate_limit,'Redis',_Factory)
    with TestClient(main.app) as c:
        r=c.post('/auth/login',json={'email':'analyst@qiyuan.demo','password':'Qiyuan2026!'})
        assert r.status_code==200,r.text
        c.headers.update({'Authorization':'Bearer '+r.json()['token']})
        yield {'client':c,'Session':Session,'benchmark_id':benchmark_id}
    engine.dispose()


@pytest.fixture()
def admin_headers(client):
    r=client['client'].post('/auth/login',json={'email':'admin@qiyuan.demo','password':'Qiyuan2026!'})
    return {'Authorization':'Bearer '+r.json()['token']}


def _drain(Session,run_id):
    with Session() as db:execute_run(db,run_id)


def _create_run(client,name='API 研究'):
    r=client['client'].post('/runs',json={'name':name,'benchmark_id':client['benchmark_id'],
                                          'inputs':['staff_hours','energy_cost'],
                                          'outputs':['visits','programs'],'sensitivity_count':2})
    assert r.status_code==201,r.text
    return r.json()


def test_full_generation_api_flow(client,admin_headers):
    run=_create_run(client)
    assert run['generation_no']==1
    _drain(client['Session'],run['id'])
    detail=client['client'].get(f"/runs/{run['id']}")
    assert detail.json()['status']=='completed'
    gens=client['client'].get(f"/runs/{run['id']}/generations").json()
    assert [g['generation_no'] for g in gens]==[1]
    assert gens[0]['status']=='published'
    # Queue generation 2; the published result stays visible and reported as current.
    r=client['client'].post(f"/runs/{run['id']}/generations",json={'sensitivity_count':2,'note':'重解'})
    assert r.status_code==201
    gen2=r.json()['generation_id']
    listed=client['client'].get(f"/runs/{run['id']}/generations").json()
    names=[g['generation_no'] for g in listed]
    assert names==[1,2]
    assert listed[0]['is_published'] is True
    assert listed[1]['status']=='queued'
    # Visibility: generation 2 parts are exposed with their failure scope field.
    detail2=client['client'].get(f"/runs/{run['id']}/generations/{gen2}").json()
    assert detail2['result']=={}          # not published, so no result body
    assert detail2['failure_scope']==[]
    assert detail2['part_total']==len(listed[1]['part_counts']) or detail2['part_total']>0
    _drain(client['Session'],run['id'])
    diff=client['client'].get(f"/runs/{run['id']}/diff").json()
    assert diff['from']['generation_no']==1 and diff['to']['generation_no']==2
    assert diff['to']['verification']['ok'] is True
    assert len(diff['venues'])==12
    published=client['client'].get(f"/runs/{run['id']}").json()
    assert published['published_generation_no']==2


def test_publish_endpoint_requires_ready_and_admin(client,admin_headers):
    run=_create_run(client)
    r=client['client'].post(f"/runs/{run['id']}/generations",json={'sensitivity_count':2})
    gen2=r.json()['generation_id']
    # Analyst cannot force-publish.
    assert client['client'].post(f"/runs/{run['id']}/generations/{gen2}/publish").status_code==403
    # Admin cannot publish a generation that has not passed per-venue verification.
    res=client['client'].post(f"/runs/{run['id']}/generations/{gen2}/publish",headers=admin_headers)
    assert res.status_code==409


def test_retry_endpoint_resumes_failed_generation(client,monkeypatch):
    run=_create_run(client)
    import src.services.generation_service as gs
    real=gs.pipeline.solve_baseline
    state={'broken':True}
    def flaky(payload,observation_id):
        record=next(r for r in payload['records'] if r['observation_id']==observation_id)
        if state['broken'] and record['venue_code']=='V-02':raise RuntimeError('中断')
        return real(payload,observation_id)
    monkeypatch.setattr(gs.pipeline,'solve_baseline',flaky)
    _drain(client['Session'],run['id'])
    gens=client['client'].get(f"/runs/{run['id']}/generations").json()
    assert gens[0]['status']=='failed'
    assert [f['venue_code'] for f in gens[0]['failure_scope']]==['V-02']
    assert client['client'].get(f"/runs/{run['id']}").json()['status']=='failed'
    state['broken']=False
    r=client['client'].post(f"/runs/{run['id']}/generations/{gens[0]['id']}/retry")
    assert r.status_code==202
    _drain(client['Session'],run['id'])
    detail=client['client'].get(f"/runs/{run['id']}/generations").json()[0]
    assert detail['status']=='published'
    assert detail['failure_scope']==[]
    assert client['client'].get(f"/runs/{run['id']}").json()['status']=='completed'


def test_case_api_pins_generation_and_migrates(client,admin_headers):
    run=_create_run(client)
    _drain(client['Session'],run['id'])
    target=client['client'].get(f"/runs/{run['id']}").json()['result']['baseline'][0]
    r=client['client'].post('/cases',json={'title':'API 假设','run_id':run['id'],
        'generation_id':run['generation_id'],'observation_id':target['observation_id'],
        'hypothesis':'先核对统计边界与开馆排程。','verification':'两周现场观察并复核口径。'})
    assert r.status_code==201,r.text
    case=r.json()
    r=client['client'].post(f"/runs/{run['id']}/generations",json={'sensitivity_count':2})
    gen2=r.json()['generation_id']
    _drain(client['Session'],run['id'])
    listed=client['client'].get('/cases').json()
    case_row=next(c for c in listed if c['id']==case['id'])
    assert case_row['is_generation_current'] is False
    assert case_row['source_generation_no']==1
    assert case_row['source_evidence']['theta']==target['theta']
    r=client['client'].post(f"/cases/{case['id']}/migrate",json={'generation_id':gen2})
    assert r.status_code==200,r.text
    migrated=r.json()
    assert migrated['source_generation_id']==gen2
    assert len(migrated['migration_history'])==1
    assert migrated['migration_history'][0]['from']['generation_no']==1
    # Cannot migrate back to the older generation.
    r=client['client'].post(f"/cases/{case['id']}/migrate",json={'generation_id':run['generation_id']})
    assert r.status_code==422
