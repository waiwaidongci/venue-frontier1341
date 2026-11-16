from sqlalchemy import ForeignKey,String,JSON,DateTime
from sqlalchemy.orm import Mapped,mapped_column
from datetime import datetime
from src.config.database import Base
class ImprovementCase(Base):
    __tablename__='improvement_cases'
    id:Mapped[int]=mapped_column(primary_key=True)
    title:Mapped[str]=mapped_column(String(160),unique=True)
    run_id:Mapped[int]=mapped_column(ForeignKey('efficiency_runs.id'))
    # The generation whose evidence the hypothesis was formed on. Never silently rewritten.
    source_generation_id:Mapped[int|None]=mapped_column(ForeignKey('run_generations.id'),nullable=True)
    observation_id:Mapped[int]=mapped_column(ForeignKey('period_observations.id'))
    hypothesis:Mapped[str]=mapped_column(String(1200))
    verification:Mapped[str]=mapped_column(String(1000))
    status:Mapped[str]=mapped_column(String(20),default='draft')
    approved_by:Mapped[str]=mapped_column(String(160),default='')
    # Evidence frozen at creation, so a superseded generation still explains the case.
    source_evidence:Mapped[dict]=mapped_column(JSON,default=dict)
    # Deliberate analyst migration to a newer generation keeps the prior evidence chain.
    migration_history:Mapped[list]=mapped_column(JSON,default=list)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
