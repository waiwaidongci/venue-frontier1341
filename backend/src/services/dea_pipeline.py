"""Deterministic, part-level DEA pipeline used by generation-based recompute.

Each venue is one verifiable part: the baseline solve does not depend on other
targets, so it can be checkpointed and resumed. The sensitivity selection is a
single deterministic aggregate over completed baseline parts; each target's
scenario scores then form a second part. Pure functions keep verification and
tests independent from the database.
"""
import hashlib,json
import numpy as np
from src.services.dea_solver import solve_dmu
from src.constants.enums import DEFAULT_ALGORITHM,SOLVER_STATUSES

REMOVAL_POLICY='same_R_minus_b_for_all_original_targets'
SENSITIVITY_POLICY='所有原目标统一使用R减去同一被移除标杆；不再逐目标排除自身'


def canonical_json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def snapshot_payload(benchmark,records,inputs,outputs,sensitivity_count,algorithm=DEFAULT_ALGORITHM):
    """Key set and ordering intentionally match the v1 input fingerprint so that
    generations created before this upgrade keep valid stored fingerprints."""
    return {'algorithm':algorithm,
            'benchmark':{'id':benchmark['id'],'code':benchmark['code'],'period':benchmark['period']},
            'records':records,
            'inputs':list(inputs),'outputs':list(outputs),
            'sensitivity_count':int(sensitivity_count),
            'baseline_self':True,
            'removal_policy':REMOVAL_POLICY}


def snapshot_fingerprint(payload):
    return digest(payload)


def venue_fingerprint(payload,record):
    return digest({'algorithm':payload['algorithm'],
                   'benchmark':payload['benchmark'],
                   'inputs':payload['inputs'],'outputs':payload['outputs'],
                   'sensitivity_count':payload['sensitivity_count'],
                   'baseline_self':payload['baseline_self'],
                   'removal_policy':payload['removal_policy'],
                   'record':record})


def selection_fingerprint(selection):
    return digest({k:selection[k] for k in ('policy','selected','sensitivity_count') if k in selection})


def sensitivity_fingerprint(payload,record,selection):
    return digest({'venue':venue_fingerprint(payload,record),
                   'snapshot':snapshot_fingerprint(payload),
                   'selection':selection_fingerprint(selection)})


def _matrices(payload):
    records=payload['records'];inputs=payload['inputs'];outputs=payload['outputs']
    x=np.array([[r[f] for f in inputs] for r in records],float)
    y=np.array([[r[f] for f in outputs] for r in records],float)
    xs=x.max(axis=0);ys=np.maximum(y.max(axis=0),1.)
    return records,x,y,xs,ys


def _record_index(payload,observation_id):
    for i,record in enumerate(payload['records']):
        if record['observation_id']==observation_id:return i,record
    raise ValueError(f'观察 {observation_id} 不在本代样本中')


def solve_baseline(payload,observation_id):
    records,x,y,xs,ys=_matrices(payload);i,record=_record_index(payload,observation_id)
    solution=solve_dmu(x[i],y[i],x,y,xs,ys)
    if solution['status']!='optimal':
        raise ValueError(f"基线求解失败 {record['venue_code']}: {solution['status']}")
    inputs,outputs=payload['inputs'],payload['outputs']
    solution['observation_id']=record['observation_id']
    solution['venue_code']=record['venue_code'];solution['venue_name']=record['venue_name']
    solution['source']={f:record[f] for f in inputs+outputs}
    solution['peers']=[{'observation_id':records[j]['observation_id'],
                        'venue_code':records[j]['venue_code'],
                        'venue_name':records[j]['venue_name'],'lambda':float(weight)}
                       for j,weight in enumerate(solution.pop('weights')) if weight>1e-7]
    solution['input_slack']=dict(zip(inputs,solution['input_slack']))
    solution['output_slack']=dict(zip(outputs,solution['output_slack']))
    solution['projected_inputs']=dict(zip(inputs,solution['projected_inputs']))
    solution['projected_outputs']=dict(zip(outputs,solution['projected_outputs']))
    solution['theta']=float(solution['theta'])
    return solution


def select_benchmarks(payload,baseline_results,sensitivity_count):
    """Same rule as v1: accumulate external lambda, rank by value then venue code."""
    influence={r['observation_id']:0. for r in payload['records']}
    for target in baseline_results:
        for peer in target.get('peers',[]):
            if peer['observation_id']!=target['observation_id']:
                influence[peer['observation_id']]+=peer['lambda']
    records=payload['records']
    selected=sorted([r for r in records if influence[r['observation_id']]>1e-7],
                    key=lambda r:(-influence[r['observation_id']],r['venue_code']))[:sensitivity_count]
    payload_selection={'policy':SENSITIVITY_POLICY,
                       'sensitivity_count':int(sensitivity_count),
                       'influence':{str(k):float(v) for k,v in influence.items()},
                       'selected':[{'observation_id':r['observation_id'],'venue_code':r['venue_code'],
                                    'venue_name':r['venue_name'],
                                    'aggregate_external_lambda':float(influence[r['observation_id']])}
                                   for r in selected]}
    payload_selection['fingerprint']=selection_fingerprint(payload_selection)
    return payload_selection


def solve_sensitivity(payload,observation_id,selection,baseline_result):
    """Solve every selected removal scenario for one target; statuses are kept."""
    records,x,y,xs,ys=_matrices(payload);i,record=_record_index(payload,observation_id)
    base_theta=baseline_result['theta'];scenarios=[]
    for removed in selection['selected']:
        keep=[j for j,r in enumerate(records) if r['observation_id']!=removed['observation_id']]
        if not keep:raise ValueError('移除后参照集为空')
        solution=solve_dmu(x[i],y[i],x[keep],y[keep],xs,ys)
        theta=solution['theta']
        if theta is not None and theta<base_theta-1e-6:
            raise ValueError('移除参照后theta不应下降，请核验求解器')
        scenarios.append({'removed_observation_id':removed['observation_id'],
                          'status':solution['status'],
                          'theta':None if theta is None else float(theta),
                          'delta':None if theta is None else float(theta-base_theta),
                          'removed_target_self':record['observation_id']==removed['observation_id']})
    return {'observation_id':record['observation_id'],'venue_code':record['venue_code'],
            'base_theta':float(base_theta),'scenarios':scenarios}


def recompute_theta(payload,observation_id):
    """Independent re-derivation used by verification (does not reuse part code paths)."""
    records,x,y,xs,ys=_matrices(payload);i,_=_record_index(payload,observation_id)
    solution=solve_dmu(x[i],y[i],x,y,xs,ys)
    if solution['status']!='optimal':raise ValueError(f"复算状态{solution['status']}")
    return float(solution['theta'])


def _scales(payload):
    records,x,y,xs,ys=_matrices(payload)
    return dict(zip(payload['inputs'],xs.tolist())),dict(zip(payload['outputs'],ys.tolist()))


def verify_generation(payload,selection,baseline_parts,sensitivity_parts):
    """Per-venue verification of score, solver feasibility state and sample fingerprint.

    baseline_parts / sensitivity_parts map observation_id -> part dict
    ({status, input_fingerprint, result, error}). Nothing is published unless every
    venue passes every check.
    """
    venues=[];issues=[]
    expected={r['observation_id']:r for r in payload['records']}
    selected_ids=[s['observation_id'] for s in selection.get('selected',[])]
    expected_scenarios=[{'removed_observation_id':s['observation_id'],'removed_code':s['venue_code'],
                         'removed_name':s['venue_name'],
                         'aggregate_external_lambda':s['aggregate_external_lambda'],
                         'reference_ids':[rid for rid in expected if rid!=s['observation_id']]}
                        for s in selection.get('selected',[])]
    for observation_id,record in expected.items():
        checks=[]
        base=baseline_parts.get(observation_id);sens=sensitivity_parts.get(observation_id)
        wanted=venue_fingerprint(payload,record);wanted_sens=sensitivity_fingerprint(payload,record,selection)
        if not base:checks.append(('baseline_part_present',False,'缺少基线分片'))
        else:
            checks.append(('baseline_part_present',True,''))
            checks.append(('baseline_part_completed',base['status']=='completed',base.get('error','') or f"状态{base['status']}"))
            checks.append(('sample_fingerprint',base['input_fingerprint']==wanted,'基线样本指纹与冻结快照不符'))
            body=base.get('result') or {}
            checks.append(('baseline_status_optimal',body.get('status')=='optimal',f"求解状态{body.get('status')}"))
            theta=body.get('theta')
            checks.append(('baseline_theta_finite',isinstance(theta,(int,float)) and np.isfinite(theta) and theta>=0,'θ缺失或非法'))
            # Score is re-derived from the frozen snapshot, never trusted from storage alone.
            try:
                recomputed=recompute_theta(payload,observation_id)
                checks.append(('baseline_theta_recomputed',
                               isinstance(theta,(int,float)) and bool(np.isfinite(theta))
                               and abs(recomputed-theta)<1e-7,
                               '存储θ与按冻结样本复算的θ不一致'))
            except Exception as exc:
                checks.append(('baseline_theta_recomputed',False,f'复算失败：{str(exc)[:200]}'))
            ids=set(expected)
            peers=body.get('peers') or []
            checks.append(('baseline_peers_in_sample',all(p.get('observation_id') in ids for p in peers),'参照机构不在样本内'))
            checks.append(('baseline_lambda_nonnegative',all(isinstance(p.get('lambda'),(int,float)) and p['lambda']>=0 for p in peers),'λ为负或非数值'))
        if not sens:checks.append(('sensitivity_part_present',False,'缺少敏感性分片'))
        else:
            checks.append(('sensitivity_part_present',True,''))
            checks.append(('sensitivity_part_completed',sens['status']=='completed',sens.get('error','') or f"状态{sens['status']}"))
            checks.append(('sensitivity_sample_fingerprint',sens['input_fingerprint']==wanted_sens,'敏感性样本/标杆选择指纹不符'))
            body=sens.get('result') or {}
            checks.append(('sensitivity_target_matches',body.get('observation_id')==observation_id,'分片目标与观察不符'))
            scenarios=body.get('scenarios') or []
            checks.append(('sensitivity_scenario_count',[s.get('removed_observation_id') for s in scenarios]==selected_ids,'情景或顺序与标杆选择不符'))
            for scenario in scenarios:
                removed=scenario.get('removed_observation_id')
                status=scenario.get('status');theta=scenario.get('theta')
                checks.append((f'feasibility_state_{removed}_explicit',status in SOLVER_STATUSES,f'未知求解状态{status}'))
                checks.append((f'theta_null_iff_not_optimal_{removed}',(theta is None)==(status!='optimal'),'非最优状态不得填θ'))
                if theta is not None:
                    checks.append((f'theta_finite_{removed}',bool(np.isfinite(theta)) and theta>=0,'θ非法'))
                    base_theta=(base or {}).get('result',{}).get('theta')
                    expected_delta=None if base_theta is None else theta-base_theta
                    checks.append((f'delta_consistent_{removed}',scenario.get('delta') is not None and abs(scenario['delta']-expected_delta)<1e-9,'Δθ与基线θ不一致'))
                checks.append((f'removed_self_flag_{removed}',scenario.get('removed_target_self')==(removed==observation_id),'被移除目标标记不符'))
            present={s.get('removed_observation_id') for s in scenarios}
            for scenario in expected_scenarios:
                removed=scenario['removed_observation_id']
                if removed not in present:checks.append((f'scenario_present_{removed}',False,'缺少情景分片'))
        failed=[{'check':name,'detail':detail} for name,ok,detail in checks if not ok]
        if failed:issues.extend({'observation_id':observation_id,'venue_code':record['venue_code'],**item} for item in failed)
        venues.append({'observation_id':observation_id,'venue_code':record['venue_code'],
                       'venue_name':record['venue_name'],'ok':not failed,
                       'checks':len(checks),'failed':failed,
                       'input_fingerprint':wanted,
                       'sensitivity_fingerprint':wanted_sens,
                       'baseline_theta':(base or {}).get('result',{}).get('theta'),
                       'sensitivity_statuses':{str(s.get('removed_observation_id')):s.get('status')
                                                for s in (sens or {}).get('result',{}).get('scenarios',[])}})
    return {'ok':not issues,'checked_venues':len(venues),'issues':issues,'venues':venues,
            'policy':'逐馆核对基线θ、求解可行状态、样本与情景指纹；全部通过才发布',
            'scenario_count':len(expected_scenarios)}


def assemble_result(payload,selection,baseline_parts,sensitivity_parts,benchmark_name):
    records=payload['records'];inputs,outputs=payload['inputs'],payload['outputs']
    baseline=[baseline_parts[r['observation_id']]['result'] for r in records]
    scores_by_target={oid:(part.get('result') or {}).get('scenarios',[])
                      for oid,part in sensitivity_parts.items()}
    scenarios=[]
    for removed in selection.get('selected',[]):
        rid=removed['observation_id']
        scenario_scores=[]
        for record in records:
            score=next((s for s in scores_by_target.get(record['observation_id'],[])
                        if s.get('removed_observation_id')==rid),None)
            if score is None:raise ValueError(f"缺少目标{record['venue_code']}对标杆{removed['venue_code']}的情景分数")
            scenario_scores.append({'observation_id':record['observation_id'],
                                    'venue_code':record['venue_code'],
                                    'status':score['status'],'theta':score['theta'],
                                    'delta':score['delta'],
                                    'removed_target_self':score['removed_target_self']})
        scenarios.append({'removed_observation_id':rid,'removed_code':removed['venue_code'],
                          'removed_name':removed['venue_name'],
                          'aggregate_external_lambda':removed['aggregate_external_lambda'],
                          'reference_ids':[r['observation_id'] for r in records if r['observation_id']!=rid],
                          'scores':scenario_scores})
    input_scales,output_scales=_scales(payload)
    return {'algorithm':payload['algorithm'],'baseline':baseline,'scenarios':scenarios,
            'input_scales':input_scales,'output_scales':output_scales,
            'reference_ids':[r['observation_id'] for r in records],
            'self_included_baseline':True,'sensitivity_policy':SENSITIVITY_POLICY,
            'n':len(records),'inputs':inputs,'outputs':outputs,
            'efficient_count':sum(bool(r.get('strong_efficient')) for r in baseline),
            'mean_theta':float(np.mean([r['theta'] for r in baseline])),
            'fingerprint':snapshot_fingerprint(payload),'period':payload['benchmark']['period'],
            'benchmark_name':benchmark_name}
