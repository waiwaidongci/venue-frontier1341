import time
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import RunGeneration
from src.constants.enums import GenerationStatus
from src.services.generation_service import execute_generation
redis=Redis.from_url(REDIS_URL)


def next_work(db):
    """Oldest active generation. Research order is generation id order, and within a
    research only the newest generation can publish (checked at publish time)."""
    gen=db.scalar(select(RunGeneration).where(
        RunGeneration.status.in_([GenerationStatus.QUEUED,GenerationStatus.RUNNING]))
        .order_by(RunGeneration.id))
    return gen.id if gen else None

while True:
    Path('/tmp/heartbeat').touch()
    with Session() as db:
        work=next_work(db)
        if work:execute_generation(db,work)
        else:redis.brpop('frontier:runs',timeout=2)
    time.sleep(.1)
