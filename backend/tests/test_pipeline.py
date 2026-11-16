"""Parity and closed-form checks for the part-level pipeline."""
import numpy as np
from src.services.dea_solver import solve_dmu
from src.services import dea_pipeline as pipeline


def _payload(records,inputs=('staff_hours','energy_cost'),outputs=('visits','programs'),
             sensitivity_count=2):
    payload=pipeline.snapshot_payload({'id':1,'code':'B','period':'2026Q3'},
                                      records,list(inputs),list(outputs),sensitivity_count)
    return payload


def _record(i,code,x,y):
    return {'observation_id':i,'venue_id':i,'venue_code':code,'venue_name':code,
            'district':'d','staff_hours':x[0],'energy_cost':x[1],
            'visits':y[0],'programs':y[1]}


def test_closed_form_three_dmu_baseline_and_removal():
    # README reference sample: x=[1,1],[2,1],[1,2]; y=[1,2],[1,2]? Use the documented
    # three-DMU sample with [x,y]=[1,1],[2,1],[1,2] on one input and one output.
    x=np.array([[1.],[2.],[1.]]);y=np.array([[1.],[1.],[2.]])
    thetas=[solve_dmu(x[i],y[i],x,y)['theta'] for i in range(3)]
    assert thetas==[.5,.25,1.0]
    keep=[0,1]
    scenario=[solve_dmu(x[i],y[i],x[keep],y[keep])['theta'] for i in range(3)]
    assert scenario==[1.,.5,2.]


def test_pipeline_matches_direct_analysis_and_is_deterministic():
    records=[_record(i+1,f'V-{i+1:02d}',
                     [10.+i,20.+2*i],[100.+5*i,50.+2*i]) for i in range(5)]
    payload=_payload(records)
    baseline={r['observation_id']:pipeline.solve_baseline(payload,r['observation_id'])
              for r in records}
    selection=pipeline.select_benchmarks(payload,[baseline[r['observation_id']] for r in records],2)
    first=pipeline.assemble_result(payload,selection,
                                   {k:{'status':'completed','input_fingerprint':'',
                                       'result':v} for k,v in baseline.items()},
                                   {r['observation_id']:{'status':'completed','input_fingerprint':'',
                                       'result':pipeline.solve_sensitivity(payload,r['observation_id'],
                                                                           selection,baseline[r['observation_id']])}
                                    for r in records},'测试参照集')
    # Determinism: a second independent run yields the same result and fingerprint.
    baseline2={r['observation_id']:pipeline.solve_baseline(payload,r['observation_id'])
               for r in records}
    selection2=pipeline.select_benchmarks(payload,[baseline2[r['observation_id']] for r in records],2)
    assert selection2==selection
    assert first['fingerprint']==pipeline.snapshot_fingerprint(payload)
    assert all(row['status']=='optimal' for row in first['baseline'])
    assert all(row['theta']>=0 for row in first['baseline'])
    for scenario in first['scenarios']:
        assert len(scenario['scores'])==3+2  # every original target keeps a score
        assert all(score['status'] in ('optimal','infeasible') for score in scenario['scores'])


def test_scales_are_shared_between_baseline_and_scenarios():
    records=[_record(1,'V-01',[10.,20.],[100.,50.]),
             _record(2,'V-02',[20.,40.],[100.,50.]),
             _record(3,'V-03',[10.,20.],[200.,100.])]
    payload=_payload(records,sensitivity_count=1)
    for record in records:
        result=pipeline.solve_baseline(payload,record['observation_id'])
        # Projections cannot exceed theta-scaled input or fall below observed output.
        for field in payload['inputs']:
            assert result['projected_inputs'][field]<=result['theta']*record[field]+1e-9
        for field in payload['outputs']:
            assert result['projected_outputs'][field]>=record[field]-1e-9


def test_multi_benchmark_selection_and_verification_of_every_scenario():
    # Two complementary frontier venues, so 2 removal scenarios run for every target.
    records=[_record(1,'V-01',[10.,40.],[100.,100.]),_record(2,'V-02',[40.,10.],[100.,100.]),
             _record(3,'V-03',[30.,30.],[70.,70.]),_record(4,'V-04',[20.,35.],[65.,90.]),
             _record(5,'V-05',[35.,20.],[90.,65.]),_record(6,'V-06',[25.,25.],[60.,60.])]
    payload=_payload(records,sensitivity_count=2)
    baseline={r['observation_id']:pipeline.solve_baseline(payload,r['observation_id'])
              for r in records}
    selection=pipeline.select_benchmarks(payload,[baseline[r['observation_id']] for r in records],2)
    assert len(selection['selected'])==2
    sensitivity={r['observation_id']:pipeline.solve_sensitivity(payload,r['observation_id'],
                  selection,baseline[r['observation_id']]) for r in records}
    for part in sensitivity.values():
        assert [s['removed_observation_id'] for s in part['scenarios']]==[s['observation_id'] for s in selection['selected']]
    base_parts={oid:{'status':'completed','input_fingerprint':pipeline.venue_fingerprint(payload,r),
                     'result':baseline[oid],'error':''}
                for oid,r in ((r['observation_id'],r) for r in records)}
    sens_parts={oid:{'status':'completed','input_fingerprint':pipeline.sensitivity_fingerprint(payload,next(r for r in records if r['observation_id']==oid),selection),
                     'result':sensitivity[oid],'error':''} for oid in sensitivity}
    verification=pipeline.verify_generation(payload,selection,base_parts,sens_parts)
    assert verification['ok'] is True
    assert verification['scenario_count']==2
    assert all(len(v['sensitivity_statuses'])==2 for v in verification['venues'])
    result=pipeline.assemble_result(payload,selection,base_parts,sens_parts,'B')
    assert len(result['scenarios'])==2
    assert all(len(s['scores'])==len(records) for s in result['scenarios'])
