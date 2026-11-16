import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, Station, StationDirective
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import stations
from src.schemas.station import StationInput
from src.utils.security import hash_password
from src.utils.serialization import checksum


def seed():
    migrate()
    with Session() as db:
        for role, name in [('admin', '林知 · 业务负责人'), ('analyst', '陈舟 · 分析员'), ('viewer', '周禾 · 观察员')]:
            email = role + '@demo.local'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name=name, role=role, password=hash_password('Demo123!')))
        db.flush()
        m = metadata()
        d = db.scalar(select(Dataset).where(Dataset.name == m['dataset_name']))
        if not d:
            rows = seed_records()
            d = Dataset(name=m['dataset_name'], source=m['short'] + '-202611.csv', records=rows,
                        checksum=checksum(rows), created_at=time.time())
            db.add(d)
            db.flush()
        admin = db.scalar(select(User).where(User.role == 'admin'))
        job = db.scalar(select(Job).where(Job.name == '基线复核 · ' + m['name']))
        if not job:
            params = {p['key']: p['default'] for p in m.get('parameters', [])}
            result = analyze(d.records, params)
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            job = Job(name='基线复核 · ' + m['name'], dataset_id=d.id, params=params, status='completed',
                      result=result, created_by=admin.id, created_at=time.time() - 3600,
                      finished_at=time.time() - 3595, approved=True)
            db.add(job)
            db.flush()
            r = Report(name='每日业务复核简报', job_id=job.id, destination='业务复核组 · 本地收件箱',
                       interval_seconds=86400, next_run=time.time() + 86400, enabled=True)
            db.add(r)
            db.flush()
            deliver(db, r)
            db.add(Audit(actor='seed', action='demo.baseline', detail={'dataset': d.id}, at=time.time()))
        # A second approved estimate (different model) lets the operator rehearse
        # publishing a newer version while an older directive is in flight.
        extra = [('AC-12 独立拟合 · ' + m['name'], 'AC-12', 0.9),
                 ('AC-12 高可靠度复核 · ' + m['name'], 'AC-12', 0.95)]
        for name, model, reliability in extra:
            if db.scalar(select(Job).where(Job.name == name)):
                continue
            params = {p['key']: p['default'] for p in m.get('parameters', [])}
            params['model'] = model
            params['reliability'] = reliability
            result = analyze(d.records, params)
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            job_extra = Job(name=name, dataset_id=d.id, params=params, status='completed',
                            result=result, created_by=admin.id, created_at=time.time() - 1800,
                            finished_at=time.time() - 1795, approved=True)
            db.add(job_extra)
            db.flush()
        db.flush()
        # Three depots.  All start in sync with the approved baseline; the drill
        # switches (receipt loss / held directives) are turned on by the operator
        # from the station console before the next publish.
        demos = [
            ('DEPOT-A', '鹭湾一号检修站'),
            ('DEPOT-B', '鹭湾二号检修站'),
            ('DEPOT-C', '鹭湾三号检修站'),
        ]
        for code, name in demos:
            station = db.scalar(select(Station).where(Station.code == code))
            if not station:
                station = stations.create_station(db, StationInput(code=code, name=name))
                db.flush()
            if not db.scalar(select(StationDirective).where(StationDirective.station_id == station.id)):
                directives = stations.publish(db, job.id, [station.id], admin)
                db.flush()
                stations.send_now(db, directives)
        db.commit()
        counts = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                  for model in [User, Dataset, Job, Report, Delivery, Station, StationDirective, Audit]}
        print(json.dumps(counts, ensure_ascii=False))
        return counts


if __name__ == '__main__':
    seed()
