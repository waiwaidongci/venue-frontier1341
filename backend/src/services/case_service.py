import json,csv,io
from datetime import datetime
from sqlalchemy import select
from fastapi import HTTPException
from src.models import ImprovementCase,EfficiencyRun,RunGeneration
from src.constants.enums import CaseStatus,GenerationStatus

def _generation_evidence(gen,row):
    return {'generation_id':gen.id,'generation_no':gen.generation_no,
            'snapshot_fingerprint':gen.snapshot_fingerprint,
            'published_at':gen.published_at.isoformat() if gen.published_at else None,
            'observation_id':row['observation_id'],'venue_code':row['venue_code'],
            'venue_name':row['venue_name'],'theta':row['theta'],
            'strong_efficient':row.get('strong_efficient'),
            'input_slack':row.get('input_slack'),'output_slack':row.get('output_slack'),
            'projected_inputs':row.get('projected_inputs'),
            'projected_outputs':row.get('projected_outputs'),'peers':row.get('peers'),
            'period':gen.result.get('period'),
            'baseline_status':row.get('status','optimal')}

def _published_generation(db,run,generation_id):
    gen=db.get(RunGeneration,generation_id) if generation_id else None
    if gen:
        if gen.run_id!=run.id:raise HTTPException(422,'研究代不属于该研究')
        if gen.status!=GenerationStatus.PUBLISHED:raise HTTPException(422,'只能引用已发布的研究代')
        return gen
    gen=db.get(RunGeneration,run.published_generation_id) if run.published_generation_id else None
    if not gen or gen.status!=GenerationStatus.PUBLISHED:raise HTTPException(422,'该研究尚无已发布的研究代')
    return gen

def _row_for(gen,observation_id):
    row=next((r for r in (gen.result or {}).get('baseline',[])
              if r['observation_id']==observation_id),None)
    if not row:raise HTTPException(422,'机构观察不属于该研究代的同期目标集')
    return row

def list_cases(db):
    out=[]
    for item in db.scalars(select(ImprovementCase).order_by(ImprovementCase.id.desc())).all():
        run=db.get(EfficiencyRun,item.run_id)
        gen=db.get(RunGeneration,item.source_generation_id) if item.source_generation_id else None
        out.append({'id':item.id,'title':item.title,'run_id':item.run_id,
                    'source_generation_id':item.source_generation_id,
                    'source_generation_no':gen.generation_no if gen else None,
                    'current_generation_id':run.published_generation_id if run else None,
                    'is_generation_current':bool(run and item.source_generation_id==run.published_generation_id),
                    'observation_id':item.observation_id,'hypothesis':item.hypothesis,
                    'verification':item.verification,'status':item.status,
                    'approved_by':item.approved_by,'created_at':item.created_at,
                    'source_evidence':item.source_evidence or {},
                    'migration_history':item.migration_history or []})
    return out

def create_case(db,data):
    run=db.get(EfficiencyRun,data.run_id)
    if not run:raise HTTPException(422,'请选择已完成研究')
    gen=_published_generation(db,run,getattr(data,'generation_id',None))
    row=_row_for(gen,data.observation_id)
    if db.scalar(select(ImprovementCase).where(ImprovementCase.title==data.title)):raise HTTPException(409,'假设标题已存在')
    payload=data.model_dump(exclude={'generation_id'})
    item=ImprovementCase(**payload,status=CaseStatus.DRAFT,
                         source_generation_id=gen.id,
                         source_evidence=_generation_evidence(gen,row),
                         migration_history=[])
    db.add(item);db.commit();return item

def approve_case(db,id,user):
    item=db.get(ImprovementCase,id)
    if not item:raise HTTPException(404,'评议假设不存在')
    item.status=CaseStatus.APPROVED;item.approved_by=user.email;db.commit();return item

def migrate_case(db,id,generation_id,user):
    """Explicitly move a case's evidence reference forward, keeping both snapshots."""
    item=db.get(ImprovementCase,id)
    if not item:raise HTTPException(404,'评议假设不存在')
    run=db.get(EfficiencyRun,item.run_id)
    if not run:raise HTTPException(404,'研究不存在')
    target=db.get(RunGeneration,generation_id)
    if not target or target.run_id!=run.id:raise HTTPException(422,'研究代不属于该研究')
    if target.status!=GenerationStatus.PUBLISHED:raise HTTPException(422,'只能迁移到已发布的研究代')
    source=db.get(RunGeneration,item.source_generation_id) if item.source_generation_id else None
    if source and target.generation_no<=source.generation_no:
        raise HTTPException(422,'只能迁移到更晚的已发布研究代')
    row=_row_for(target,item.observation_id)
    history=list(item.migration_history or [])
    history.append({'migrated_at':datetime.utcnow().isoformat(),'migrated_by':user.email,
                    'from':item.source_evidence or ({'generation_id':item.source_generation_id}),
                    'to':_generation_evidence(target,row)})
    item.migration_history=history
    item.source_generation_id=target.id
    item.source_evidence=_generation_evidence(target,row)
    db.commit();return item

def export_case(db,id):
    item=db.get(ImprovementCase,id)
    if not item:raise HTTPException(404,'评议假设不存在')
    if item.status!=CaseStatus.APPROVED:raise HTTPException(409,'负责人批准后才能交付')
    evidence=item.source_evidence or {}
    if not evidence:
        # Defensive: pre-migration rows without frozen evidence read the current generation.
        run=db.get(EfficiencyRun,item.run_id)
        gen=db.get(RunGeneration,item.source_generation_id) if item.source_generation_id else None
        if run and gen:
            evidence=_generation_evidence(gen,_row_for(gen,item.observation_id))
    def safe(value):
        text=str(value)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    out=io.StringIO();out.write('﻿');writer=csv.writer(out)
    writer.writerow(['venue_code','venue_name','period','theta','input_slack','output_surplus',
                     'projected_inputs','peer_lambdas','hypothesis','verification','approved_by',
                     'source_generation_no','current_generation_no','run_fingerprint','migration_count'])
    writer.writerow([safe(evidence.get('venue_code')),safe(evidence.get('venue_name')),
                     safe(evidence.get('period')),evidence.get('theta'),
                     json.dumps(evidence.get('input_slack'),ensure_ascii=False),
                     json.dumps(evidence.get('output_slack'),ensure_ascii=False),
                     json.dumps(evidence.get('projected_inputs'),ensure_ascii=False),
                     json.dumps(evidence.get('peers'),ensure_ascii=False),
                     safe(item.hypothesis),safe(item.verification),safe(item.approved_by),
                     evidence.get('generation_no'),item.migration_history[-1]['to']['generation_no'] if item.migration_history else evidence.get('generation_no'),
                     evidence.get('snapshot_fingerprint'),len(item.migration_history or [])])
    return out.getvalue()
