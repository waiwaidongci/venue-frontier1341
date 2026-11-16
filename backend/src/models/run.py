from sqlalchemy import ForeignKey,String,JSON,DateTime
from sqlalchemy.orm import Mapped,mapped_column
from datetime import datetime
from src.config.database import Base
class EfficiencyRun(Base):
    __tablename__='efficiency_runs'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(160),unique=True)
    benchmark_id:Mapped[int]=mapped_column(ForeignKey('benchmark_sets.id'))
    inputs:Mapped[list]=mapped_column(JSON)
    outputs:Mapped[list]=mapped_column(JSON)
    sensitivity_count:Mapped[int]=mapped_column(default=3)
    algorithm:Mapped[str]=mapped_column(String(80),default='ccr-input-two-stage-v1')
    status:Mapped[str]=mapped_column(String(20),default='queued')
    # Mirrors the published generation only; partial generation work never lands here.
    result:Mapped[dict]=mapped_column(JSON,default=dict)
    error:Mapped[str]=mapped_column(String(600),default='')
    # Plain integer (no FK) to keep the schema free of a circular constraint.
    published_generation_id:Mapped[int|None]=mapped_column(nullable=True)
    # Monotonic publish marker used by the compare-and-swap in publish_generation.
    published_generation_no:Mapped[int|None]=mapped_column(nullable=True)
    generation_count:Mapped[int]=mapped_column(default=0)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
