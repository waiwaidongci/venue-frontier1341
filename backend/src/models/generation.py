from sqlalchemy import ForeignKey,String,JSON,DateTime,Integer,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from datetime import datetime
from src.config.database import Base
class RunGeneration(Base):
    """One recompute generation of a research (EfficiencyRun).

    A queued generation is computed part-by-part, verified, and then published in
    one transaction. Until that publish, the previously published generation keeps
    serving `EfficiencyRun.result`.
    """
    __tablename__='run_generations'
    __table_args__=(UniqueConstraint('run_id','generation_no'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    run_id:Mapped[int]=mapped_column(ForeignKey('efficiency_runs.id'),index=True)
    generation_no:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(20),default='queued',index=True)
    algorithm:Mapped[str]=mapped_column(String(80))
    note:Mapped[str]=mapped_column(String(600),default='')
    sensitivity_count:Mapped[int]=mapped_column(default=3)
    snapshot:Mapped[dict]=mapped_column(JSON,default=dict)
    snapshot_fingerprint:Mapped[str]=mapped_column(String(64),default='')
    selection:Mapped[dict]=mapped_column(JSON,default=dict)
    result:Mapped[dict]=mapped_column(JSON,default=dict)
    verification:Mapped[dict]=mapped_column(JSON,default=dict)
    failure_scope:Mapped[list]=mapped_column(JSON,default=list)
    error:Mapped[str]=mapped_column(String(600),default='')
    attempt:Mapped[int]=mapped_column(Integer,default=0)
    created_by:Mapped[str]=mapped_column(String(160),default='')
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    started_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    finished_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    published_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
class GenerationPart(Base):
    """Per-venue unit of work. Completed parts survive an interrupted generation."""
    __tablename__='generation_parts'
    __table_args__=(UniqueConstraint('generation_id','observation_id','stage'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    generation_id:Mapped[int]=mapped_column(ForeignKey('run_generations.id'),index=True)
    observation_id:Mapped[int]=mapped_column(Integer)
    venue_code:Mapped[str]=mapped_column(String(60))
    venue_name:Mapped[str]=mapped_column(String(140))
    stage:Mapped[str]=mapped_column(String(20),default='baseline')
    status:Mapped[str]=mapped_column(String(20),default='pending',index=True)
    input_fingerprint:Mapped[str]=mapped_column(String(64),default='')
    result:Mapped[dict]=mapped_column(JSON,default=dict)
    error:Mapped[str]=mapped_column(String(600),default='')
    attempt:Mapped[int]=mapped_column(Integer,default=0)
    started_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    completed_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
