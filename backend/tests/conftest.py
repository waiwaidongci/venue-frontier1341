import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from src.config.database import Base
import src.services.generation_service as generation_service


class _NullQueue:
    def lpush(self,*args,**kwargs):return 1
    def brpop(self,*args,**kwargs):return None


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    # Queue wake-ups are best effort; tests drive generations synchronously.
    monkeypatch.setattr(generation_service,'redis',_NullQueue())


def build_db(path=':memory:'):
    engine=create_engine(f'sqlite:///{path}',connect_args={'check_same_thread':False},
                         poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    return engine,Session


def seed_users(Session):
    from src.models import User
    from src.utils.security import hash_password
    with Session() as db:
        for role,name in [('admin','许栖 · 评议负责人'),('analyst','沈原 · 场馆分析员'),
                          ('viewer','何禾 · 机构联络员')]:
            db.add(User(email=f'{role}@qiyuan.demo',name=name,role=role,
                        password_hash=hash_password('Qiyuan2026!')))
        db.commit()


def seed_sample(Session,n=12,period='2026Q3'):
    """Deterministic sample: 3 frontier venues and the rest interior."""
    seed_users(Session)
    with Session() as db:
        from src.models import BenchmarkSet,Venue,PeriodObservation
        bench=BenchmarkSet(code=f'B-{period}',name=f'测试参照集 {period}',period=period,source_file='test.csv')
        db.add(bench);db.flush()
        for i in range(n):
            venue=Venue(code=f'V-{i+1:02d}',name=f'场馆{i+1:02d}',district='东岸片区')
            db.add(venue);db.flush()
            efficient=i<3
            scale=1.0+0.15*(i%4)
            if efficient:
                staff,energy,visits,programs=10*scale,20*scale,100*scale,50*scale
            else:
                staff=10*scale*(1.6-0.02*i)
                energy=20*scale*(1.7-0.02*i)
                visits=100*scale*(0.75+0.01*i)
                programs=50*scale*(0.8+0.008*i)
            db.add(PeriodObservation(benchmark_id=bench.id,venue_id=venue.id,
                                     staff_hours=round(max(staff,1e-3),4),
                                     energy_cost=round(max(energy,1e-3),4),
                                     visits=round(max(visits,0),4),
                                     programs=round(max(programs,0),4)))
        db.commit()
        return bench.id
